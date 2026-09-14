"""Private, disposable progress for native Slides reads; never evidence itself.

One completed slide response is the atomic unit. The adapter must establish a
fresh revision/order binding before load and recheck it before returning output.
No metadata, source text or exception payload is logged by this helper.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
from pathlib import Path


SAFE_ERROR_CODES = frozenset({
    "document_resume_cache_unsafe", "document_resume_cache_invalid",
    "document_resume_cache_budget_exceeded", "document_resume_write_failed",
    "document_resume_busy", "document_resume_invalidated",
    "document_resume_revision_required",
})


class ResumeError(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def _validate(info, *, directory=False):
    good_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (not good_type or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077
            or (not directory and info.st_nlink != 1)):
        raise ResumeError("document_resume_cache_unsafe")


class NativeSlidesResume:
    """A per-account/file lock plus bounded, checksummed contiguous slide prefix."""
    NAMES = {"read.lock", "checkpoint.json", "checkpoint.tmp", "discard.requested"}

    def __init__(self, home, account, file_id, *, text_budget):
        self.home = Path(home).expanduser().absolute()
        self.identity = {"account": account, "file_id": file_id}
        namespace = _hash(_json(self.identity))
        self.path = self.home / "document_cache" / "native_slides" / namespace
        self.text_budget = text_budget
        self.cache_budget = 2 * text_budget + 1024 * 1024
        self.directory_fd = None
        self.lock_fd = None
        self.binding = None
        self.parts = []

    def _prepare(self, *, create=True):
        try:
            # Reject symlink components before opening; never resolve/chmod an
            # arbitrary caller-supplied private home into apparent compliance.
            for path in (self.home, *self.home.parents):
                if path.is_symlink():
                    raise ResumeError("document_resume_cache_unsafe")
            _validate(self.home.lstat(), directory=True)
            for path in (self.home / "document_cache", self.path.parent, self.path):
                if create:
                    path.mkdir(mode=0o700, exist_ok=True)
                _validate(path.lstat(), directory=True)
            self.directory_fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            _validate(os.fstat(self.directory_fd), directory=True)
            if set(os.listdir(self.directory_fd)) - self.NAMES:
                raise ResumeError("document_resume_cache_unsafe")
            for name in os.listdir(self.directory_fd):
                _validate(os.stat(name, dir_fd=self.directory_fd, follow_symlinks=False))
        except ResumeError:
            self.close()
            raise
        except FileNotFoundError:
            self.close()
            if not create:
                return False
            raise ResumeError("document_resume_cache_unsafe") from None
        except OSError:
            self.close()
            raise ResumeError("document_resume_cache_unsafe") from None
        return True

    def _open(self, name, flags, *, missing=False):
        try:
            fd = os.open(name, flags | os.O_NOFOLLOW, 0o600, dir_fd=self.directory_fd)
            try:
                _validate(os.fstat(fd))
            except BaseException:
                os.close(fd)
                raise
            return fd
        except FileNotFoundError:
            if missing:
                return None
            raise

    def _exists(self, name):
        try:
            _validate(os.stat(name, dir_fd=self.directory_fd, follow_symlinks=False))
            return True
        except FileNotFoundError:
            return False

    def _unlink(self, name):
        if self._exists(name):
            os.unlink(name, dir_fd=self.directory_fd)

    def _lock(self):
        self.lock_fd = self._open("read.lock", os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ResumeError("document_resume_busy") from None

    def __enter__(self):
        try:
            self._prepare()
            self._lock()
            # Interrupted atomic writes never become resumable checkpoints.
            self._unlink("checkpoint.tmp")
            if self._exists("discard.requested"):
                self._unlink("checkpoint.json")
                self._unlink("discard.requested")
            return self
        except ResumeError:
            self.close()
            raise
        except OSError:
            self.close()
            raise ResumeError("document_resume_cache_unsafe") from None

    def _check_discard(self):
        if self._exists("discard.requested"):
            self.clear(consume_discard=True)
            raise ResumeError("document_resume_invalidated")

    def load(self, binding):
        """Call only after the adapter's fresh metadata and complete inventory."""
        self._check_discard()
        self.binding = {**self.identity, **binding}
        try:
            if len(_json(self.binding)) > 1024 * 1024:
                raise ResumeError("document_resume_cache_budget_exceeded")
            fd = self._open("checkpoint.json", os.O_RDONLY, missing=True)
            if fd is None:
                return []
            with os.fdopen(fd, "rb") as handle:
                if os.fstat(handle.fileno()).st_size > self.cache_budget:
                    raise ResumeError("document_resume_cache_budget_exceeded")
                raw = handle.read(self.cache_budget + 1)
            if len(raw) > self.cache_budget:
                raise ResumeError("document_resume_cache_budget_exceeded")
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError
                    result[key] = value
                return result
            value = json.loads(raw, object_pairs_hook=unique)
            if (not isinstance(value, dict) or set(value) != {"kind", "version", "binding", "binding_hash", "parts"}
                    or value["kind"] != "cha_philosophy_native_slides_progress" or type(value["version"]) is not int
                    or value["version"] != 1 or not isinstance(value["binding"], dict)
                    or any(value["binding"].get(k) != v for k, v in self.identity.items())
                    or value["binding_hash"] != _hash(_json(value["binding"]))):
                raise ValueError
            if value["binding"] != self.binding:
                self.clear()  # A known own cache, but another revision or order.
                return []
            records = value["parts"]
            ids = binding["slide_ids"]
            if not isinstance(records, list) or len(records) > len(ids):
                raise ValueError
            parts = []
            byte_count = 0
            for number, record in enumerate(records, 1):
                if (not isinstance(record, dict) or set(record) != {"number", "slide_id", "text", "sha256"}
                        or type(record["number"]) is not int or record["number"] != number
                        or record["slide_id"] != ids[number - 1] or not isinstance(record["text"], str)
                        or record["sha256"] != _hash(record["text"].encode("utf-8"))):
                    raise ValueError
                byte_count += len(record["text"].encode("utf-8")) + 2
                if byte_count > self.text_budget:
                    raise ResumeError("document_resume_cache_budget_exceeded")
                parts.append(record["text"])
            self.parts = parts
            return list(parts)
        except ResumeError:
            raise
        except (OSError, ValueError, TypeError, KeyError, UnicodeError):
            raise ResumeError("document_resume_cache_invalid") from None

    def append(self, slide_id, text):
        """Persist only a fully validated slide; failed writes preserve old prefix."""
        self._check_discard()
        if (self.binding is None or len(self.parts) >= len(self.binding["slide_ids"])
                or slide_id != self.binding["slide_ids"][len(self.parts)] or not isinstance(text, str)):
            raise ResumeError("document_resume_cache_invalid")
        parts = [*self.parts, text]
        if sum(len(part.encode("utf-8")) + 2 for part in parts) > self.text_budget:
            raise ResumeError("document_resume_cache_budget_exceeded")
        records = [{"number": i, "slide_id": self.binding["slide_ids"][i - 1],
                    "text": part, "sha256": _hash(part.encode("utf-8"))} for i, part in enumerate(parts, 1)]
        raw = _json({"kind": "cha_philosophy_native_slides_progress", "version": 1,
                     "binding": self.binding, "binding_hash": _hash(_json(self.binding)), "parts": records})
        if len(raw) > self.cache_budget:
            raise ResumeError("document_resume_cache_budget_exceeded")
        try:
            self._unlink("checkpoint.tmp")
            fd = self._open("checkpoint.tmp", os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            self._check_discard()
            self._exists("checkpoint.json")  # Do not replace a symlink/hardlink.
            os.replace("checkpoint.tmp", "checkpoint.json", src_dir_fd=self.directory_fd, dst_dir_fd=self.directory_fd)
            os.fsync(self.directory_fd)
            self.parts = parts
        except ResumeError:
            raise
        except OSError:
            raise ResumeError("document_resume_write_failed") from None
        finally:
            self._unlink("checkpoint.tmp")

    def clear(self, *, consume_discard=False):
        names = ["checkpoint.json", "checkpoint.tmp"]
        if consume_discard:
            names.append("discard.requested")
        for name in names:
            self._unlink(name)
        self.parts = []
        os.fsync(self.directory_fd)

    def finish(self):
        """Only after final metadata/order checks, remove disposable source text."""
        self._check_discard()
        self.clear()
        # Cleanup must not consume an invalidation arriving after its first
        # check. This is the completion boundary while we still hold the lock.
        self._check_discard()

    def discard(self):
        """Source access loss may arrive while another process holds the lock."""
        try:
            if not self._prepare(create=False):
                return {"state": "discarded"}
            fd = self._open("discard.requested", os.O_WRONLY | os.O_CREAT)
            os.fsync(fd)
            os.close(fd)
            os.fsync(self.directory_fd)
            try:
                self._lock()
            except ResumeError as exc:
                if exc.code == "document_resume_busy":
                    return {"state": "discard_pending"}
                raise
            self.clear(consume_discard=True)
            return {"state": "discarded"}
        except ResumeError:
            raise
        except OSError:
            raise ResumeError("document_resume_write_failed") from None
        finally:
            self.close()

    def close(self):
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None
        if self.directory_fd is not None:
            os.close(self.directory_fd)
            self.directory_fd = None

    def __exit__(self, *_):
        self.close()
