"""No live login, network, existing credentials, D-Bus or keyring writes."""
import io
import base64
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tools.cha_philosophy.teams_auth import (
    AuthBinding, DeviceLoginPrompt, SCOPES, SecretServicePersistence,
    TeamsAuth, TeamsAuthError, _encrypted_session, auth_status,
)

CLIENT = "11111111-1111-4111-8111-111111111111"
TENANT = "22222222-2222-4222-8222-222222222222"
PROFESSOR = "33333333-3333-4333-8333-333333333333"
OTHER = "44444444-4444-4444-8444-444444444444"
TOKEN = "token-canary-never-log"
CODE = "ABCD-EFGH"


def config():
    return {"teams_auth": {"mode": "device_code", "client_id": CLIENT},
            "teams_tenant_id": TENANT, "teams_professor_ids": [PROFESSOR]}


def account(**changes):
    return {"home_account_id": PROFESSOR + "." + TENANT,
            "local_account_id": PROFESSOR, "realm": TENANT,
            "environment": "login.microsoftonline.com", **changes}


def token_result(**changes):
    return {"access_token": TOKEN, "scope": " ".join(SCOPES), "expires_in": 3600,
            "id_token_claims": {"tid": TENANT, "oid": PROFESSOR}, **changes}


class TeamsAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.app = Mock()
        self.app.get_accounts.return_value = [account()]
        self.app.acquire_token_silent_with_error.return_value = token_result()
        self.app.initiate_device_flow.return_value = {
            "device_code": "PRIVATE-device-code", "user_code": CODE,
            "verification_uri": "https://malicious.invalid/token-canary-never-log",
            "message": "raw-provider-message-with-secrets",
        }
        self.app.acquire_token_by_device_flow.return_value = token_result()
        self.persistence = Mock(is_encrypted=True)
        self.factory = Mock(return_value=(self.app, self.persistence))
        self.graph = Mock()
        self.graph.get_json.return_value = {"id": PROFESSOR}
        self.graph_factory = Mock(return_value=self.graph)
        self.now = 1000.0
        self.auth = TeamsAuth(config(), self.home, backend_factory=self.factory,
                              graph_factory=self.graph_factory, clock=lambda: self.now)

    def tearDown(self):
        self.temp.cleanup()

    def bind(self, **changes):
        self.auth._paths(create=True)
        data = {"version": 1, **self.auth.binding.metadata(),
                "home_account_id": account()["home_account_id"], **changes}
        self.auth.binding_path.write_text(json.dumps(data))
        os.chmod(self.auth.binding_path, 0o600)

    def assert_code(self, code, operation):
        with self.assertRaises(TeamsAuthError) as raised:
            operation()
        self.assertEqual(str(raised.exception), code)
        self.assertTrue(raised.exception.__suppress_context__ or raised.exception.__context__ is None)
        return raised.exception

    def test_status_and_missing_binding_never_open_backend_or_start_login(self):
        self.assertEqual(self.auth.status()["state"], "login_required")
        self.assert_code("teams_auth_login_required", self.auth.acquire_silent)
        self.factory.assert_not_called()
        self.app.initiate_device_flow.assert_not_called()
        self.assertFalse((self.home / "teams_auth").exists())
        self.bind()
        self.assertEqual(self.auth.status()["state"], "configured_unverified")
        self.factory.assert_not_called()

    def test_config_rejects_unbound_accounts_tenant_urls_and_unsupported_mode(self):
        bad = [
            {**config(), "teams_professor_ids": [PROFESSOR, OTHER]},
            {**config(), "teams_professor_ids": ["display name"]},
            {**config(), "teams_tenant_id": "common"},
            {**config(), "teams_tenant_id": "https://attacker.invalid"},
            {**config(), "teams_auth": {"mode": "env", "client_id": CLIENT}},
        ]
        for value in bad:
            with self.subTest(value=value):
                self.assert_code("teams_auth_config_invalid", lambda: TeamsAuth(value, self.home))
        self.assertEqual(auth_status({}, self.home)["state"], "not_configured")

    def test_silent_uses_bound_account_and_every_required_scope(self):
        self.bind()
        self.assertEqual(self.auth.acquire_silent(), TOKEN)
        self.app.acquire_token_silent_with_error.assert_called_once_with(
            list(SCOPES), account=account(), force_refresh=False)
        self.graph.get_json.assert_called_once_with("/me?$select=id")
        self.app.initiate_device_flow.assert_not_called()
        self.persistence.close.assert_called_once()

    def test_validated_token_reused_until_expiry_margin_then_silent_reacquired(self):
        self.bind()
        for _ in range(10):
            self.assertEqual(self.auth.acquire_silent(), TOKEN)
        self.assertEqual(self.graph.get_json.call_count, 1)
        self.now += 3300
        self.assertEqual(self.auth.acquire_silent(), TOKEN)
        self.assertEqual(self.graph.get_json.call_count, 2)
        self.assertEqual(self.app.acquire_token_silent_with_error.call_count, 2)

    def test_force_refresh_validates_new_token_and_failure_does_not_reuse_old(self):
        self.bind()
        self.auth.acquire_silent()
        self.app.acquire_token_silent_with_error.return_value = token_result(access_token="new-secret-token")
        self.assertEqual(self.auth.acquire_silent(force_refresh=True), "new-secret-token")
        self.assertTrue(self.app.acquire_token_silent_with_error.call_args.kwargs["force_refresh"])
        self.assertEqual(self.graph_factory.call_count, 2)
        self.app.acquire_token_silent_with_error.return_value = {"error": "invalid_grant", "error_description": TOKEN}
        self.assert_code("teams_auth_login_required", lambda: self.auth.acquire_silent(force_refresh=True))
        self.assert_code("teams_auth_login_required", self.auth.acquire_silent)

    def test_changed_local_binding_invalidates_memory_token_before_backend(self):
        self.bind()
        self.auth.acquire_silent()
        self.bind(professor_id=OTHER)
        self.assert_code("teams_auth_binding_mismatch", self.auth.acquire_silent)
        self.assertEqual(self.factory.call_count, 1)

    def test_wrong_cached_user_tenant_cloud_or_duplicate_account_stops_before_token(self):
        self.bind()
        for values in ([account(local_account_id=OTHER)], [account(realm=OTHER)],
                       [account(environment="attacker.invalid")], [account(), account()]):
            self.app.get_accounts.return_value = values
            self.assert_code("teams_auth_account_mismatch", self.auth.acquire_silent)
        self.app.acquire_token_silent_with_error.assert_not_called()

    def test_removed_own_cache_requires_login_without_starting_it(self):
        self.bind()
        self.app.get_accounts.return_value = []
        self.assert_code("teams_auth_login_required", self.auth.acquire_silent)
        self.app.acquire_token_silent_with_error.assert_not_called()
        self.app.initiate_device_flow.assert_not_called()

    def test_wrong_token_claims_or_graph_identity_are_never_bound(self):
        for changes in ({"id_token_claims": {"tid": OTHER, "oid": PROFESSOR}},
                        {"id_token_claims": {"tid": TENANT, "oid": OTHER}},
                        {"id_token_claims": None}):
            self.app.acquire_token_by_device_flow.return_value = token_result(**changes)
            self.assert_code("teams_auth_account_mismatch", lambda: self.auth.login_device_code(lambda _: None))
            self.assertFalse(self.auth.binding_path.exists())
        self.app.acquire_token_by_device_flow.return_value = token_result()
        self.graph.get_json.return_value = {"id": OTHER}
        self.assert_code("teams_auth_account_mismatch", lambda: self.auth.login_device_code(lambda _: None))
        self.assertFalse(self.auth.binding_path.exists())

    def test_missing_scope_cannot_reach_graph_or_silent_memory_cache(self):
        self.bind()
        self.app.acquire_token_silent_with_error.return_value = token_result(scope="User.Read Chat.Read")
        self.assert_code("teams_auth_scopes_missing", self.auth.acquire_silent)
        self.graph_factory.assert_not_called()
        self.app.initiate_device_flow.assert_not_called()

    def test_graph_qualified_scopes_work_and_expired_tokens_fail(self):
        self.bind()
        self.app.acquire_token_silent_with_error.return_value = token_result(
            scope=" ".join("https://graph.microsoft.com/" + s for s in SCOPES), expires_in=0)
        self.assert_code("teams_auth_login_required", self.auth.acquire_silent)

    def test_claims_challenge_stops_without_interactive_login(self):
        self.bind()
        self.app.acquire_token_silent_with_error.return_value = {
            "error": "interaction_required", "claims": '{"secret": "' + TOKEN + '"}'}
        self.assert_code("teams_auth_login_required", self.auth.acquire_silent)
        self.app.initiate_device_flow.assert_not_called()

    def test_safe_error_codes_distinguish_consent_expiry_cancellation_network(self):
        self.bind()
        for error, expected in (("consent_required", "teams_auth_consent_required"),
                                ("expired_token", "teams_auth_device_expired"),
                                ("authorization_declined", "teams_auth_cancelled"),
                                ("temporarily_unavailable", "teams_auth_network_failed")):
            self.app.acquire_token_silent_with_error.return_value = {"error": error, "error_description": TOKEN}
            self.assert_code(expected, self.auth.acquire_silent)

    def test_provider_exception_messages_never_escape(self):
        self.bind()
        self.app.acquire_token_silent_with_error.side_effect = RuntimeError(TOKEN + " https://secret.invalid")
        err = self.assert_code("teams_auth_cache_unavailable", self.auth.acquire_silent)
        self.assertNotIn(TOKEN, str(err))
        self.app.acquire_token_by_device_flow.side_effect = RuntimeError(TOKEN)
        self.assert_code("teams_auth_failed", lambda: self.auth.login_device_code(lambda _: None))

    def test_explicit_login_returns_only_safe_metadata_and_private_binding(self):
        prompts = []
        result = self.auth.login_device_code(prompts.append)
        self.assertEqual(result["state"], "authenticated")
        self.assertEqual(prompts, [DeviceLoginPrompt(CODE)])
        self.assertNotIn(CODE, repr(prompts))
        self.assertEqual(prompts[0].verification_uri, "https://microsoft.com/devicelogin")
        self.assertEqual(self.app.initiate_device_flow.call_args.kwargs["scopes"], list(SCOPES))
        serialized = json.dumps(result) + self.auth.binding_path.read_text()
        for secret in (TOKEN, CODE, "PRIVATE-device-code", "raw-provider-message", "malicious.invalid"):
            self.assertNotIn(secret, serialized)
        self.assertEqual(self.auth.binding_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.auth.directory.stat().st_mode & 0o777, 0o700)

    def test_failed_login_preserves_previous_good_binding(self):
        self.bind()
        before = self.auth.binding_path.read_bytes()
        self.app.acquire_token_by_device_flow.return_value = token_result(id_token_claims={"tid": TENANT, "oid": OTHER})
        self.assert_code("teams_auth_account_mismatch", lambda: self.auth.login_device_code(lambda _: None))
        self.assertEqual(self.auth.binding_path.read_bytes(), before)

    def test_symlinks_insecure_modes_and_namespace_swap_fail_before_backend(self):
        self.bind()
        os.chmod(self.auth.binding_path, 0o644)
        self.assert_code("teams_auth_cache_unsafe", self.auth.acquire_silent)
        os.chmod(self.auth.binding_path, 0o600)
        self.auth.binding_path.unlink()
        other = self.home / "unrelated"
        other.write_text("do not read")
        self.auth.binding_path.symlink_to(other)
        self.assert_code("teams_auth_cache_unsafe", self.auth.acquire_silent)
        self.assertEqual(other.read_text(), "do not read")
        self.factory.assert_not_called()

    def test_transient_msal_lock_mode_does_not_break_concurrent_auth_but_symlink_does(self):
        self.bind()
        lock = self.auth.directory / "cache.lock"
        lock.write_text("12345 cli")
        os.chmod(lock, 0o644)  # Actual CrossPlatLock creation follows process umask.
        self.assertEqual(self.auth.status()["state"], "configured_unverified")
        lock.unlink()
        lock.symlink_to(self.auth.binding_path)
        self.assert_code("teams_auth_cache_unsafe", self.auth.acquire_silent)
        self.factory.assert_not_called()

    def test_binding_namespace_changes_for_app_tenant_and_professor(self):
        for value in ({**config(), "teams_tenant_id": OTHER},
                      {**config(), "teams_professor_ids": [OTHER]},
                      {**config(), "teams_auth": {"mode": "device_code", "client_id": OTHER}}):
            self.assertNotEqual(AuthBinding.from_config(value).namespace, self.auth.binding.namespace)


class LoginCacheIsolationTests(unittest.TestCase):
    """Real MSAL cache mutation with fake PCA/Secret Service, never live auth."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.saved = []
        self.existing = None
        self.persistence = Mock(is_encrypted=True)
        def load():
            from msal_extensions.persistence import PersistenceNotFound
            if self.existing is None:
                raise PersistenceNotFound(message="fixture empty")
            return self.existing
        def save(value):
            self.saved.append(value)
            self.existing = value
        self.persistence.load.side_effect = load
        self.persistence.save.side_effect = save
        self.professor = PROFESSOR
        self.before_return = lambda: None
        def pca(client_id, *, token_cache, **kwargs):
            self.cache = token_cache
            app = Mock()
            app.get_accounts.side_effect = lambda: list(token_cache.search("Account"))
            app.initiate_device_flow.return_value = {"device_code": "private-device-code", "user_code": CODE}
            def obtain(_):
                response = token_result(id_token_claims={"tid": TENANT, "oid": self.professor})
                info = base64.urlsafe_b64encode(json.dumps({"uid": self.professor, "utid": TENANT}).encode()).decode()
                token_cache.add({"client_id": CLIENT, "scope": list(SCOPES),
                    "token_endpoint": "https://login.microsoftonline.com/" + TENANT + "/oauth2/v2.0/token",
                    "response": {**response, "client_info": info, "refresh_token": "refresh-token-canary"}})
                self.before_return()
                return response
            app.acquire_token_by_device_flow.side_effect = obtain
            return app
        self.pca_factory = pca
        graph = Mock()
        graph.get_json.return_value = {"id": PROFESSOR}
        self.auth = TeamsAuth(config(), self.home, graph_factory=Mock(return_value=graph))

    def tearDown(self):
        self.temp.cleanup()

    def login(self):
        with patch("tools.cha_philosophy.teams_auth.SecretServicePersistence", return_value=self.persistence), \
             patch("msal.PublicClientApplication", side_effect=self.pca_factory), \
             patch("tools.cha_philosophy.teams_auth._quiet_provider_loggers"):
            return self.auth.login_device_code(lambda _: None)

    def test_wrong_identity_never_reaches_persistence_even_with_real_msal_auto_cache(self):
        self.professor = OTHER
        self.existing = '{"Account":{"existing-good":{"local_account_id":"' + PROFESSOR + '"}}}'
        original = self.existing
        with self.assertRaisesRegex(TeamsAuthError, "^teams_auth_account_mismatch$"):
            self.login()
        self.assertIn(TOKEN, self.cache.serialize())  # Demonstrate actual in-memory MSAL automatic write.
        self.assertEqual(self.saved, [])
        self.persistence.load.assert_not_called()
        self.assertEqual(self.existing, original)
        self.assertFalse(self.auth.binding_path.exists())

    def test_verified_login_commits_once_and_preserves_concurrent_entry(self):
        # Simulate another process completing an update during device login.
        self.before_return = lambda: setattr(self, "existing", '{"AppMetadata":{"concurrent":{"client_id":"other-resource"}}}')
        result = self.login()
        self.assertEqual(result["state"], "authenticated")
        self.assertEqual(len(self.saved), 1)
        self.assertIn(TOKEN, self.existing)
        self.assertIn("refresh-token-canary", self.existing)
        self.assertIn("concurrent", json.loads(self.existing)["AppMetadata"])
        self.assertTrue(self.auth.binding_path.exists())
        self.assertFalse((self.auth.directory / "cache.lock").exists())

    def test_keyring_save_failure_leaves_login_unbound(self):
        self.persistence.save.side_effect = TeamsAuthError("teams_auth_keyring_unavailable")
        with self.assertRaisesRegex(TeamsAuthError, "^teams_auth_keyring_unavailable$"):
            self.login()
        self.assertFalse(self.auth.binding_path.exists())

    def test_different_private_homes_cannot_share_secret_with_different_locks(self):
        from tools.cha_philosophy.teams_auth import _backend_factory
        self.auth._paths(create=True)
        second_home = self.home / "second-home"
        second_home.mkdir(mode=0o700)
        second = TeamsAuth(config(), second_home)
        second._paths(create=True)
        with patch("tools.cha_philosophy.teams_auth.SecretServicePersistence", return_value=self.persistence) as constructor, \
             patch("msal.PublicClientApplication", side_effect=self.pca_factory), \
             patch("tools.cha_philosophy.teams_auth._quiet_provider_loggers"):
            _backend_factory(self.auth.binding, self.auth.directory, login=True)
            _backend_factory(second.binding, second.directory, login=True)
        locations_and_ids = [call.args for call in constructor.call_args_list]
        self.assertNotEqual(locations_and_ids[0][0], locations_and_ids[1][0])
        self.assertNotEqual(locations_and_ids[0][1], locations_and_ids[1][1])


class MsalCacheHitTests(unittest.TestCase):
    setUp = TeamsAuthTests.setUp
    tearDown = TeamsAuthTests.tearDown
    bind = TeamsAuthTests.bind
    assert_code = TeamsAuthTests.assert_code

    def test_real_msal_cache_hit_without_scope_validates_exact_cached_target(self):
        import msal
        cache = msal.SerializableTokenCache()
        info = base64.urlsafe_b64encode(json.dumps({"uid": PROFESSOR, "utid": TENANT}).encode()).decode()
        cache.add({"client_id": CLIENT, "scope": list(SCOPES),
            "token_endpoint": "https://login.microsoftonline.com/" + TENANT + "/oauth2/v2.0/token",
            "response": {**token_result(), "client_info": info, "refresh_token": "refresh-token-canary"}})
        authority = "https://login.microsoftonline.com/" + TENANT
        http = Mock()
        http.get.return_value = SimpleNamespace(status_code=200, headers={}, text=json.dumps({
            "authorization_endpoint": authority + "/oauth2/v2.0/authorize",
            "token_endpoint": authority + "/oauth2/v2.0/token",
            "issuer": authority + "/v2.0"}))
        app = msal.PublicClientApplication(CLIENT, authority=authority, token_cache=cache,
                                           http_client=http, instance_discovery=False)
        result = app.acquire_token_silent_with_error(list(SCOPES), account=app.get_accounts()[0])
        self.assertNotIn("scope", result)
        self.assertEqual(result["token_source"], "cache")
        self.bind()
        self.factory.return_value = (app, self.persistence)
        self.assertEqual(self.auth.acquire_silent(), TOKEN)
        self.graph.get_json.assert_called_once_with("/me?$select=id")
        http.post.assert_not_called()  # No forced refresh used to work around missing scope.

    def test_cache_hit_scope_recovery_rejects_different_client_or_account(self):
        import msal
        cache = msal.SerializableTokenCache()
        cache.modify("AccessToken", {"client_id": OTHER, "realm": TENANT,
            "home_account_id": account()["home_account_id"], "environment": "login.microsoftonline.com",
            "secret": TOKEN, "target": " ".join(SCOPES), "expires_on": "4102444800"}, {"token_type": "Bearer"})
        self.bind()
        self.app.token_cache = cache
        self.app.acquire_token_silent_with_error.return_value = {
            "access_token": TOKEN, "expires_in": 3600, "token_source": "cache"}
        self.assert_code("teams_auth_scopes_missing", self.auth.acquire_silent)
        self.graph_factory.assert_not_called()


class FakeCollection:
    def __init__(self):
        self.session = SimpleNamespace(encrypted=True)
        self.locked = False
        self.items = []
        self.searches = []
        self._collection = Mock()
        self._collection.call.return_value = ("/own/item", "/")

    def is_locked(self):
        return self.locked

    def search_items(self, attrs):
        self.searches.append(dict(attrs))
        return iter(self.items)


class SecretPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "cache.signal"
        self.collection = FakeCollection()
        self.persistence = SecretServicePersistence(self.path, "own-namespace", collection=self.collection)

    def tearDown(self):
        self.temp.cleanup()

    def test_constructor_and_missing_signal_do_not_read_or_write_secret_items(self):
        self.assertEqual(self.collection.searches, [])
        self.collection._collection.call.assert_not_called()
        self.assertFalse(self.path.exists())
        from msal_extensions.persistence import PersistenceNotFound
        with self.assertRaises(PersistenceNotFound):
            self.persistence.time_last_modified()

    def test_missing_collection_is_not_created_and_connection_is_closed(self):
        import secretstorage
        connection = Mock()
        with patch("secretstorage.dbus_init", return_value=connection), \
             patch("secretstorage.Collection", side_effect=secretstorage.ItemNotFoundException("missing")), \
             patch("secretstorage.get_default_collection") as automatic, \
             patch("tools.cha_philosophy.teams_auth._encrypted_session") as negotiate:
            with self.assertRaisesRegex(TeamsAuthError, "^teams_auth_keyring_collection_missing$"):
                SecretServicePersistence(self.path, "new-own-namespace")
        automatic.assert_not_called()
        negotiate.assert_not_called()
        connection.close.assert_called_once()

    def test_exact_namespace_only_and_existing_item_write_has_no_prompt_or_plaintext_file(self):
        item = Mock()
        item.is_locked.return_value = False
        item.get_secret.return_value = b'{"AccessToken":"secret"}'
        self.collection.items = [item]
        self.assertEqual(self.persistence.load(), '{"AccessToken":"secret"}')
        self.persistence.save('{"RefreshToken":"new-secret"}')
        for attrs in self.collection.searches:
            self.assertEqual(attrs, {"application": "cha-philosophy-teams", "cache_id": "own-namespace",
                                     "cache_schema": "delegated-v1"})
        item.set_secret.assert_called_once_with(b'{"RefreshToken":"new-secret"}', "application/json")
        self.collection._collection.call.assert_not_called()
        self.assertEqual(self.path.read_bytes(), b"")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_missing_secret_is_notfound_but_existing_secret_read_error_is_not(self):
        from msal_extensions.persistence import PersistenceNotFound
        with self.assertRaises(PersistenceNotFound):
            self.persistence.load()
        item = Mock()
        item.is_locked.return_value = False
        item.get_secret.side_effect = RuntimeError(TOKEN)
        self.collection.items = [item]
        with self.assertRaisesRegex(TeamsAuthError, "^teams_auth_keyring_unavailable$"):
            self.persistence.load()

    def test_locked_collection_and_locked_item_fail_without_unlock(self):
        self.collection.locked = True
        with self.assertRaisesRegex(TeamsAuthError, "^teams_auth_keyring_locked$"):
            self.persistence.save(TOKEN)
        self.assertEqual(self.collection.searches, [])
        self.collection.locked = False
        item = Mock()
        item.is_locked.return_value = True
        self.collection.items = [item]
        with self.assertRaisesRegex(TeamsAuthError, "^teams_auth_keyring_locked$"):
            self.persistence.load()
        item.get_secret.assert_not_called()
        item.unlock.assert_not_called()

    def test_plain_session_rejected_before_item_lookup(self):
        self.collection.session.encrypted = False
        with self.assertRaisesRegex(TeamsAuthError, "^teams_auth_encryption_unavailable$"):
            SecretServicePersistence(self.path, "own-namespace", collection=self.collection)
        self.assertEqual(self.collection.searches, [])

    def test_new_item_prompt_is_rejected_never_executed_or_marked_saved(self):
        self.collection._collection.call.return_value = ("/", "/prompt/requires-user")
        with patch("secretstorage.util.format_secret", return_value=("encrypted",)):
            with self.assertRaisesRegex(TeamsAuthError, "^teams_auth_prompt_required$"):
                self.persistence.save(TOKEN)
        self.assertEqual(self.collection._collection.call.call_args.args[0], "CreateItem")
        self.assertFalse(self.path.exists())

    def test_lock_race_after_preflight_never_executes_prompt(self):
        LockedException = type("LockedException", (RuntimeError,), {})
        self.collection._collection.call.side_effect = LockedException(TOKEN)
        with patch("secretstorage.util.format_secret", return_value=("encrypted",)):
            with self.assertRaisesRegex(TeamsAuthError, "^teams_auth_keyring_locked$"):
                self.persistence.save(TOKEN)
        self.assertFalse(self.path.exists())

    def test_successful_new_item_touches_only_empty_signal_after_save(self):
        with patch("secretstorage.util.format_secret", return_value=("encrypted",)):
            self.persistence.save(TOKEN)
        self.assertEqual(self.path.read_bytes(), b"")
        self.assertGreater(self.persistence.time_last_modified(), 0)

    def test_duplicate_own_items_fail_instead_of_choosing_one(self):
        self.collection.items = [Mock(), Mock()]
        with self.assertRaisesRegex(TeamsAuthError, "^teams_auth_cache_unsafe$"):
            self.persistence.load()

    def test_write_failure_does_not_update_signal(self):
        item = Mock()
        item.is_locked.return_value = False
        item.set_secret.side_effect = RuntimeError(TOKEN)
        self.collection.items = [item]
        with self.assertRaisesRegex(TeamsAuthError, "^teams_auth_keyring_unavailable$"):
            self.persistence.save(TOKEN)
        self.assertFalse(self.path.exists())

    def test_encryption_negotiation_has_no_plaintext_retry(self):
        service = Mock()
        service.call.side_effect = RuntimeError("NotSupported " + TOKEN)
        with patch("secretstorage.util.DBusAddressWrapper", return_value=service):
            with self.assertRaisesRegex(TeamsAuthError, "^teams_auth_encryption_unavailable$"):
                _encrypted_session(Mock())
        self.assertEqual(service.call.call_count, 1)
        self.assertEqual(service.call.call_args.args[2], "dh-ietf1024-sha256-aes128-cbc-pkcs7")


if __name__ == "__main__":
    unittest.main()
