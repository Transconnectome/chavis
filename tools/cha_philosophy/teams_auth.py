"""Bound delegated Teams authentication; importing/status never opens a keyring.

The only interactive entry point is ``login_device_code(display)``. Services use
``acquire_silent`` and cannot start login, unlock a collection or execute prompts.
Only a separately approved public-client ID belongs in teams_auth.client_id.

MSAL-extensions 1.3.1 LibsecretPersistence invokes a write/read/delete trial in its
constructor, and SecretStorage create_item may execute a prompt. The persistence
below instead reuses PersistedTokenCache with narrowly scoped Secret Service calls.
There is no application plaintext cache or plaintext transport fallback. Secret
Service protects the cache; the OS keyring's encryption at rest and availability
after reboot must still be verified operationally, not inferred from session DH.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from .connectors import ConnectorError, TeamsGraphClient

SCOPES = ("User.Read", "Chat.Read", "Channel.ReadBasic.All", "ChannelMessage.Read.All")
SAFE_ERROR_CODES = frozenset({
    "teams_auth_not_configured", "teams_auth_config_invalid", "teams_auth_login_required",
    "teams_auth_cache_unsafe", "teams_auth_binding_mismatch", "teams_auth_cache_unavailable",
    "teams_auth_dependencies_missing", "teams_auth_keyring_locked", "teams_auth_keyring_unavailable",
    "teams_auth_keyring_collection_missing", "teams_auth_encryption_unavailable",
    "teams_auth_prompt_required", "teams_auth_account_mismatch", "teams_auth_scopes_missing",
    "teams_auth_consent_required", "teams_auth_network_failed", "teams_auth_device_expired",
    "teams_auth_cancelled", "teams_auth_failed", "teams_auth_identity_check_failed",
})


class TeamsAuthError(ConnectorError):
    def __init__(self, code):
        super().__init__(code if code in SAFE_ERROR_CODES else "teams_auth_failed")


def _guid(value):
    try:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F-]{36}", value):
            raise ValueError
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise TeamsAuthError("teams_auth_config_invalid") from None


@dataclass(frozen=True)
class AuthBinding:
    client_id: str
    tenant_id: str
    professor_id: str

    @classmethod
    def from_config(cls, config):
        auth = config.get("teams_auth")
        if auth is None:
            raise TeamsAuthError("teams_auth_not_configured")
        if not isinstance(auth, dict) or auth.get("mode") != "device_code":
            raise TeamsAuthError("teams_auth_config_invalid")
        if not auth.get("client_id"):
            raise TeamsAuthError("teams_auth_not_configured")
        ids = config.get("teams_professor_ids")
        if not isinstance(ids, list) or len(ids) != 1:
            raise TeamsAuthError("teams_auth_config_invalid")
        return cls(_guid(auth["client_id"]),
                   _guid(config.get("teams_tenant_id")), _guid(ids[0]))

    @property
    def authority(self):
        return "https://login.microsoftonline.com/" + self.tenant_id

    @property
    def namespace(self):
        return hashlib.sha256((self.client_id + ":" + self.tenant_id + ":" + self.professor_id).encode()).hexdigest()

    def metadata(self):
        return {"client_id": self.client_id, "tenant_id": self.tenant_id, "professor_id": self.professor_id}


@dataclass(frozen=True)
class DeviceLoginPrompt:
    # A CLI may display these ONLY on its explicitly interactive terminal. Do not
    # serialize the provider flow, message, device_code, or this short-lived code.
    user_code: str = field(repr=False)
    verification_uri: str = field(default="https://microsoft.com/devicelogin", repr=False)


def _private_path(path, *, directory=False, missing=False, lock_metadata=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing:
            return False
        raise TeamsAuthError("teams_auth_login_required") from None
    except OSError:
        raise TeamsAuthError("teams_auth_cache_unsafe") from None
    safe_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not safe_type or info.st_uid != os.getuid() or (not lock_metadata and stat.S_IMODE(info.st_mode) & 0o077):
        raise TeamsAuthError("teams_auth_cache_unsafe")
    if not directory and info.st_nlink != 1:
        raise TeamsAuthError("teams_auth_cache_unsafe")
    return True


def _safe_result_error(result):
    if not isinstance(result, dict):
        return "teams_auth_login_required"
    error = result.get("error")
    if error in {"consent_required", "invalid_scope", "unauthorized_client"} or result.get("suberror") == "consent_required":
        return "teams_auth_consent_required"
    if 65001 in (result.get("error_codes") or []):
        return "teams_auth_consent_required"
    if error in {"authorization_declined", "access_denied"}:
        return "teams_auth_cancelled"
    if error in {"expired_token", "code_expired"}:
        return "teams_auth_device_expired"
    if error in {"temporarily_unavailable", "server_error"}:
        return "teams_auth_network_failed"
    return "teams_auth_login_required"


def _quiet_provider_loggers():
    # Provider DEBUG output can contain flows, URLs or response bodies even when
    # MSAL PII logging is disabled. These namespaces have no product log contract.
    for prefix in ("msal", "msal_extensions", "secretstorage", "jeepney", "urllib3", "requests"):
        names = [prefix] + [name for name in logging.Logger.manager.loggerDict if name.startswith(prefix + ".")]
        for name in names:
            logger = logging.getLogger(name)
            logger.handlers = [logging.NullHandler()]
            logger.propagate = False


def _encrypted_session(connection):
    # Reuse SecretStorage's reviewed DH/AES implementation, but do NOT call its
    # open_session helper, which negotiates a plaintext fallback on NotSupported.
    from secretstorage.dhcrypto import Session
    from secretstorage.util import DBusAddressWrapper, SS_PATH, SERVICE_IFACE, ALGORITHM_DH
    session = Session()
    service = DBusAddressWrapper(SS_PATH, SERVICE_IFACE, connection)
    try:
        output, path = service.call("OpenSession", "sv", ALGORITHM_DH,
                                    ("ay", session.my_public_key.to_bytes(128, "big")))
        signature, value = output
        if signature != "ay" or not isinstance(path, str) or path == "/":
            raise ValueError
        session.set_server_public_key(int.from_bytes(value, "big"))
        session.object_path = path
        if not session.encrypted or session.aes_key is None:
            raise ValueError
        return session
    except Exception:
        raise TeamsAuthError("teams_auth_encryption_unavailable") from None


class SecretServicePersistence:
    """MSAL-extensions persistence protocol, exact own namespace, no prompts.

    ``is_encrypted`` has the same OS-secret-store meaning as LibsecretPersistence;
    it is not independent attestation of the OS collection's disk encryption.
    Only the empty change-signal file is on disk, never token serialization.
    """
    is_encrypted = True

    def __init__(self, location, namespace, *, collection=None):
        self.location = Path(location)
        self.attributes = {"application": "cha-philosophy-teams", "cache_id": namespace,
                           "cache_schema": "delegated-v1"}
        _private_path(self.location.parent, directory=True)
        _private_path(self.location, missing=True)
        self.connection = None
        try:
            if collection is None:
                import secretstorage
                self.connection = secretstorage.dbus_init()
                # Collection reads existing default metadata; get_default_collection
                # would create a missing collection, so must never be used here.
                collection = secretstorage.Collection(self.connection)
                self._require_unlocked(collection)
                collection.session = _encrypted_session(self.connection)
            self.collection = collection
            self._require_unlocked(collection)
            if not collection.session or not collection.session.encrypted:
                raise TeamsAuthError("teams_auth_encryption_unavailable")
        except TeamsAuthError:
            self.close()
            raise
        except ImportError:
            self.close()
            raise TeamsAuthError("teams_auth_dependencies_missing") from None
        except Exception as exc:
            self.close()
            self._raise_storage_error(exc)

    @staticmethod
    def _require_unlocked(obj):
        if obj.is_locked():
            raise TeamsAuthError("teams_auth_keyring_locked")

    @staticmethod
    def _raise_storage_error(exc):
        if isinstance(exc, TeamsAuthError):
            raise exc
        # Exception types only: never inspect/echo provider messages or payloads.
        code = {"LockedException": "teams_auth_keyring_locked",
                "ItemNotFoundException": "teams_auth_keyring_collection_missing"}.get(
                    type(exc).__name__, "teams_auth_keyring_unavailable")
        raise TeamsAuthError(code) from None

    def _item(self):
        self._require_unlocked(self.collection)
        items = list(self.collection.search_items(self.attributes))
        if len(items) > 1:
            raise TeamsAuthError("teams_auth_cache_unsafe")
        if items:
            self._require_unlocked(items[0])
            return items[0]
        return None

    def load(self):
        from msal_extensions.persistence import PersistenceNotFound
        try:
            item = self._item()
            if item is None:
                raise PersistenceNotFound(message="Own Teams cache not initialized")
            value = item.get_secret()
            if len(value) > 8 * 1024 * 1024:
                raise TeamsAuthError("teams_auth_cache_unsafe")
            return value.decode("utf-8")
        except PersistenceNotFound:
            raise
        except Exception as exc:
            self._raise_storage_error(exc)

    def save(self, content):
        try:
            if not isinstance(content, str) or len(content.encode("utf-8")) > 8 * 1024 * 1024:
                raise TeamsAuthError("teams_auth_cache_unsafe")
            item = self._item()
            if item:
                item.set_secret(content.encode("utf-8"), "application/json")
            else:
                # Collection.create_item executes returned prompts automatically.
                # The raw call preserves IsLocked races as errors and never calls
                # Unlock, Prompt, create_collection or a user's browser.
                from secretstorage.util import format_secret, SS_PREFIX
                secret = format_secret(self.collection.session, content.encode("utf-8"), "application/json")
                properties = {SS_PREFIX + "Item.Label": ("s", "Cha philosophy Teams cache"),
                              SS_PREFIX + "Item.Attributes": ("a{ss}", self.attributes)}
                item_path, prompt = self.collection._collection.call(
                    "CreateItem", "a{sv}(oayays)b", properties, secret, False)
                if prompt != "/":
                    raise TeamsAuthError("teams_auth_prompt_required")
                if not isinstance(item_path, str) or item_path == "/":
                    raise TeamsAuthError("teams_auth_keyring_unavailable")
            _private_path(self.location, missing=True)
            fd = os.open(self.location, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                os.fchmod(fd, 0o600)
                os.utime(fd, None)
            finally:
                os.close(fd)
        except Exception as exc:
            self._raise_storage_error(exc)

    def time_last_modified(self):
        from msal_extensions.persistence import PersistenceNotFound
        if not _private_path(self.location, missing=True):
            raise PersistenceNotFound(message="Own Teams cache signal not initialized")
        return self.location.stat().st_mtime

    def get_location(self):
        return str(self.location)

    def close(self):
        if self.connection is not None:
            try:
                self.connection.close()
            except Exception:
                pass
            self.connection = None


class _VerifiedLoginPersistence:
    """Hold initial tokens in memory until the exact account has been checked."""
    def __init__(self, persistence, cache, lock_location):
        self.persistence = persistence
        self.cache = cache
        self.lock_location = lock_location

    def commit_login(self):
        from msal_extensions import CrossPlatLock
        from msal_extensions.persistence import PersistenceNotFound
        # Use the same lock as PersistedTokenCache, reload at commit time, and
        # merge entries instead of overwriting updates from an hourly refresh.
        with CrossPlatLock(self.lock_location):
            try:
                existing = json.loads(self.persistence.load())
            except PersistenceNotFound:
                existing = {}
            incoming = json.loads(self.cache.serialize())
            if not isinstance(existing, dict) or not isinstance(incoming, dict):
                raise TeamsAuthError("teams_auth_cache_unsafe")
            for kind, entries in incoming.items():
                if not isinstance(entries, dict) or not isinstance(existing.get(kind, {}), dict):
                    raise TeamsAuthError("teams_auth_cache_unsafe")
                existing.setdefault(kind, {}).update(entries)
            self.persistence.save(json.dumps(existing))

    def close(self):
        self.persistence.close()


def _backend_factory(binding, directory, *, login=False):
    _quiet_provider_loggers()
    try:
        import msal
        from msal_extensions import PersistedTokenCache
    except ImportError:
        raise TeamsAuthError("teams_auth_dependencies_missing") from None
    # Different --home directories must not share one secret with independent
    # locks/signals; that would allow a cold second cache to overwrite the first.
    storage_namespace = hashlib.sha256((binding.namespace + ":" + str(directory.resolve())).encode()).hexdigest()
    persistence = SecretServicePersistence(directory / "cache.signal", storage_namespace)
    try:
        lock_location = str(directory / "cache.lock")
        cache = (msal.SerializableTokenCache() if login else
                 PersistedTokenCache(persistence, lock_location=lock_location))
        if not login and cache.is_encrypted is not True:
            raise TeamsAuthError("teams_auth_encryption_unavailable")
        app = msal.PublicClientApplication(
            binding.client_id, authority=binding.authority, token_cache=cache,
            enable_pii_log=False, enable_broker_on_linux=False,
            enable_broker_on_windows=False, enable_broker_on_mac=False,
            enable_broker_on_wsl=False, instance_discovery=False, timeout=30)
        return app, (_VerifiedLoginPersistence(persistence, cache, lock_location) if login else persistence)
    except Exception:
        persistence.close()
        raise


class TeamsAuth:
    def __init__(self, config, private_home, *, backend_factory=None, graph_factory=None, clock=time.monotonic):
        self.binding = AuthBinding.from_config(config)
        self.home = Path(private_home).expanduser().absolute()
        self.root = self.home / "teams_auth"
        self.directory = self.root / self.binding.namespace
        self.binding_path = self.directory / "binding.json"
        self.backend_factory = backend_factory or _backend_factory
        self.graph_factory = graph_factory or TeamsGraphClient
        self.clock = clock
        self._validated_token = None
        self._validated_until = 0.0
        self._validated_binding = None

    def _paths(self, *, create=False):
        # Reject symlink traversal even when the final private directory looks safe.
        for path in reversed((self.home, *self.home.parents)):
            if path.is_symlink():
                raise TeamsAuthError("teams_auth_cache_unsafe")
        _private_path(self.home, directory=True)
        for path in (self.root, self.directory):
            if create:
                path.mkdir(mode=0o700, exist_ok=True)
            _private_path(path, directory=True)
        for name in ("binding.json", "binding.new", "cache.signal"):
            _private_path(self.directory / name, missing=True)
        # MSAL-extensions creates transient PID/argv-only lock files using the
        # process umask. Accept their modes inside this verified 0700 directory
        # so a concurrent process can wait for the same lock. No token is here;
        # symlink, foreign-owner, special-file and hardlink checks still apply.
        _private_path(self.directory / "cache.lock", missing=True, lock_metadata=True)

    def _read_binding(self):
        self._paths()
        _private_path(self.binding_path)
        try:
            if self.binding_path.stat().st_size > 8192:
                raise ValueError
            data = json.loads(self.binding_path.read_text())
            if any(data.get(k) != v for k, v in self.binding.metadata().items()):
                raise TeamsAuthError("teams_auth_binding_mismatch")
            if data.get("version") != 1 or not re.fullmatch(r"[A-Za-z0-9._-]{1,1024}", data.get("home_account_id", "")):
                raise ValueError
            return data
        except TeamsAuthError:
            raise
        except Exception:
            raise TeamsAuthError("teams_auth_binding_mismatch") from None

    def status(self):
        """Local binding presence only; no cache, authentication, D-Bus or network."""
        try:
            self._read_binding()
            return {"state": "configured_unverified", "interactive": False,
                    "cache_backend": "os_secret_service", "live_auth_checked": False}
        except TeamsAuthError as exc:
            return {"state": "login_required" if exc.code == "teams_auth_login_required" else "error",
                    "error_code": exc.code, "interactive": False, "live_auth_checked": False}

    def _accounts(self, app, home_account_id=None):
        matches = []
        accounts = app.get_accounts()
        if not accounts:
            raise TeamsAuthError("teams_auth_login_required")
        for account in accounts:
            if not isinstance(account, dict):
                continue
            if (str(account.get("local_account_id", "")).lower() == self.binding.professor_id
                    and str(account.get("realm", "")).lower() == self.binding.tenant_id
                    and account.get("environment", "").lower() == "login.microsoftonline.com"
                    and (home_account_id is None or account.get("home_account_id") == home_account_id)):
                matches.append(account)
        if len(matches) != 1:
            raise TeamsAuthError("teams_auth_account_mismatch")
        return matches[0]

    def _verify_result(self, result, *, initial=False, cache=None, home_account_id=None):
        if not isinstance(result, dict) or not result.get("access_token"):
            raise TeamsAuthError(_safe_result_error(result))
        token = result["access_token"]
        if not isinstance(token, str) or not token.strip() or re.search(r"[\r\n]", token):
            raise TeamsAuthError("teams_auth_failed")
        claimed = result.get("id_token_claims")
        if initial or claimed is not None:
            if (not isinstance(claimed, dict) or str(claimed.get("tid", "")).lower() != self.binding.tenant_id
                    or str(claimed.get("oid", "")).lower() != self.binding.professor_id):
                raise TeamsAuthError("teams_auth_account_mismatch")
        scopes = result.get("scope", "")
        if not scopes and not initial and result.get("token_source") == "cache" and cache is not None:
            # MSAL 1.34 cache-hit responses omit scope. Check the actual selected
            # cache credential, not a guessed scope or an unbound token's claim.
            entries = list(cache.search("AccessToken", target=list(SCOPES), query={
                "client_id": self.binding.client_id, "realm": self.binding.tenant_id,
                "home_account_id": home_account_id, "environment": "login.microsoftonline.com",
                "secret": token}))
            if len(entries) != 1:
                raise TeamsAuthError("teams_auth_scopes_missing")
            scopes = entries[0].get("target", "")
        if not isinstance(scopes, str):
            raise TeamsAuthError("teams_auth_scopes_missing")
        actual = {s.lower().removeprefix("https://graph.microsoft.com/") for s in scopes.split()}
        if not {s.lower() for s in SCOPES}.issubset(actual):
            raise TeamsAuthError("teams_auth_scopes_missing")
        try:
            me = self.graph_factory(token).get_json("/me?$select=id")
            if not isinstance(me, dict) or str(me.get("id", "")).lower() != self.binding.professor_id:
                raise TeamsAuthError("teams_auth_account_mismatch")
        except TeamsAuthError:
            raise
        except ConnectorError as exc:
            code = {"graph_network_failed": "teams_auth_network_failed",
                    "teams_auth_required": "teams_auth_login_required",
                    "teams_access_denied": "teams_auth_consent_required"}.get(exc.code, "teams_auth_identity_check_failed")
            raise TeamsAuthError(code) from None
        return token

    def acquire_silent(self, *, force_refresh=False):
        """Return a token to the Graph adapter only; never log/serialize the result."""
        persistence = None
        try:
            if type(force_refresh) is not bool:
                raise TeamsAuthError("teams_auth_config_invalid")
            saved = self._read_binding()  # No cache discovery when login not bound.
            if (not force_refresh and self._validated_token and saved == self._validated_binding
                    and self.clock() < self._validated_until):
                return self._validated_token
            self._validated_token = None
            self._validated_until = 0.0
            app, persistence = self.backend_factory(self.binding, self.directory)
            account = self._accounts(app, saved["home_account_id"])
            result = app.acquire_token_silent_with_error(list(SCOPES), account=account,
                                                        force_refresh=force_refresh)
            token = self._verify_result(result, cache=app.token_cache,
                                        home_account_id=saved["home_account_id"])
            remaining = result.get("expires_in")
            if type(remaining) in (int, float) and 0 < remaining < float("inf"):
                # Expiry comes from MSAL, not an assumed provider token lifetime.
                self._validated_until = self.clock() + max(0, remaining - 300)
                self._validated_token = token
                self._validated_binding = saved
            elif remaining is not None:
                raise TeamsAuthError("teams_auth_login_required")
            return token
        except TeamsAuthError:
            self._validated_token = None
            raise
        except Exception as exc:
            self._validated_token = None
            code = "teams_auth_network_failed" if type(exc).__name__ in {
                "ConnectionError", "ConnectTimeout", "ReadTimeout", "Timeout", "TimeoutError"} else "teams_auth_cache_unavailable"
            raise TeamsAuthError(code) from None
        finally:
            if persistence is not None:
                persistence.close()

    def login_device_code(self, display):
        """Explicit CLI invocation only; display must be a private terminal sink."""
        if not callable(display):
            raise TeamsAuthError("teams_auth_config_invalid")
        persistence = None
        try:
            self._paths(create=True)
            app, persistence = self.backend_factory(self.binding, self.directory, login=True)
            flow = app.initiate_device_flow(scopes=list(SCOPES))
            if not isinstance(flow, dict) or not flow.get("device_code"):
                raise TeamsAuthError(_safe_result_error(flow))
            code = flow.get("user_code", "")
            if not isinstance(code, str) or not re.fullmatch(r"[A-Z0-9-]{4,32}", code):
                raise TeamsAuthError("teams_auth_failed")
            # Never display provider message, verification_uri_complete or its URL.
            display(DeviceLoginPrompt(code))
            result = app.acquire_token_by_device_flow(flow)
            self._verify_result(result, initial=True)
            account = self._accounts(app)
            home_id = account.get("home_account_id", "")
            if not isinstance(home_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,1024}", home_id):
                raise TeamsAuthError("teams_auth_account_mismatch")
            persistence.commit_login()
            data = {"version": 1, **self.binding.metadata(), "home_account_id": home_id}
            target = self.directory / "binding.new"
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                with os.fdopen(fd, "w") as handle:
                    json.dump(data, handle, sort_keys=True)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(target, self.binding_path)
            finally:
                if target.exists():
                    target.unlink()
            return {"state": "authenticated", "account_bound": True, "cache_backend": "os_secret_service",
                    "scopes": list(SCOPES), "interactive": True}
        except TeamsAuthError:
            raise
        except KeyboardInterrupt:
            raise TeamsAuthError("teams_auth_cancelled") from None
        except Exception:
            raise TeamsAuthError("teams_auth_failed") from None
        finally:
            if persistence is not None:
                persistence.close()


def auth_status(config, private_home):
    try:
        return TeamsAuth(config, private_home).status()
    except TeamsAuthError as exc:
        return {"state": "not_configured" if exc.code == "teams_auth_not_configured" else "error",
                "error_code": exc.code, "interactive": False, "live_auth_checked": False}
