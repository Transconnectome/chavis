"""Interrupted Slides reads cannot become evidence or cross revision/account scope."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.cha_philosophy.connectors import ConnectorError, GogClient
from tools.cha_philosophy.document_resume import NativeSlidesResume, ResumeError


MIME = "application/vnd.google-apps.presentation"
META = {"id": "file-a", "mimeType": MIME, "modifiedTime": "2026-09-13T00:00:00Z"}


def inventory(ids=("slide-1", "slide-2", "slide-3")):
    return {"presentationId": META["id"], "slideCount": len(ids),
            "slides": [{"objectId": sid} for sid in ids]}


class FakeSlides(GogClient):
    def __init__(self, home=None, *, account="account-a", ids=None):
        super().__init__(account, document_cache_home=home)
        self.ids = list(ids or ["slide-1", "slide-2", "slide-3"])
        self.metadata = dict(META)
        self.reads = []
        self.events = []
        self.failure = None
        self.bad_response = None
        self.metadata_failure = None
        self.final_metadata_failure = None
        self.final_inventory = None
        self.meta_reads = 0
        self.inventory_reads = 0
        self.on_slide = None

    def get_drive_metadata(self, fid):
        self.events.append("metadata")
        self.meta_reads += 1
        if self.metadata_failure:
            raise self.metadata_failure
        if self.meta_reads > 1 and self.final_metadata_failure:
            raise self.final_metadata_failure
        return dict(self.metadata)

    def _call(self, command, args, **kwargs):
        if command == ("slides", "list-slides"):
            self.events.append("inventory")
            self.inventory_reads += 1
            return inventory(self.final_inventory if self.inventory_reads > 1 and self.final_inventory is not None else self.ids)
        if command == ("drive", "download"):
            raise ConnectorError("gog_native_export_timeout")
        if command != ("slides", "read-slide"):
            raise AssertionError("unexpected command")
        sid = args[1]
        self.events.append("slide:" + sid)
        self.reads.append(sid)
        if self.failure and self.failure[0] == sid:
            raise self.failure[1]
        response = {"presentationId": META["id"], "slideObjectId": sid,
                    "slideNumber": self.ids.index(sid) + 1, "notes": "note " + sid,
                    "textElements": [{"text": "body " + sid}]}
        if self.bad_response and self.bad_response[0] == sid:
            response.update(self.bad_response[1])
        if self.on_slide:
            self.on_slide(sid)
        return response

    def read(self):
        return self._read_native_slide_text(dict(self.metadata), inventory(self.ids))


class SlideResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def cache(self, account="account-a", file_id="file-a"):
        return NativeSlidesResume(self.home, account, file_id, text_budget=32 * 1024 * 1024)

    def checkpoint(self, account="account-a"):
        return self.cache(account).path / "checkpoint.json"

    def partial(self, *, failure=None, account="account-a"):
        client = FakeSlides(self.home, account=account)
        client.failure = ("slide-2", failure or ConnectorError("gog_read_failed"))
        with self.assertRaises(BaseException):
            client.read()
        return client

    def test_mid_file_interruption_resumes_only_completed_units_and_matches_full_read(self):
        first = self.partial(failure=KeyboardInterrupt())
        self.assertEqual(first.reads, ["slide-1", "slide-2"])
        saved = json.loads(self.checkpoint().read_text())
        self.assertEqual([r["slide_id"] for r in saved["parts"]], ["slide-1"])
        self.assertEqual(self.checkpoint().stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.checkpoint().parent.stat().st_mode & 0o777, 0o700)
        resumed = FakeSlides(self.home)
        result = resumed.read()
        self.assertEqual(resumed.events[:2], ["metadata", "inventory"])
        self.assertEqual(resumed.reads, ["slide-2", "slide-3"])
        self.assertEqual(resumed.events[-2:], ["inventory", "metadata"])
        self.assertEqual(result["extracted_text"], FakeSlides().read()["extracted_text"])
        self.assertEqual(result["extraction"]["resumed_slide_count"], 1)
        self.assertTrue(result["extraction"]["resume_revision_and_order_rechecked"])
        self.assertFalse(self.checkpoint().exists())

    def test_read_document_export_fallback_uses_optional_cache(self):
        client = FakeSlides(self.home)
        result = client.read_document(META)
        self.assertEqual(result["extraction"]["slide_count"], 3)
        self.assertEqual(client.meta_reads, 2)
        self.assertEqual(client.inventory_reads, 3)  # Before export and both resume boundaries.
        self.assertFalse(self.checkpoint().exists())

    def test_no_cache_argument_preserves_existing_behavior_and_leaves_no_files(self):
        client = FakeSlides()
        result = client.read()
        self.assertNotIn("resumed_slide_count", result["extraction"])
        self.assertEqual(client.meta_reads, 1)
        self.assertEqual(client.inventory_reads, 1)
        self.assertEqual(client.discard_document_cache("file-a"), {"state": "disabled"})
        self.assertEqual(list(self.home.iterdir()), [])

    def test_invalid_response_is_not_saved_despite_body_read(self):
        client = FakeSlides(self.home)
        client.bad_response = ("slide-2", {"slideNumber": 1})
        with self.assertRaisesRegex(ConnectorError, "^document_slide_response_invalid$"):
            client.read()
        self.assertEqual(len(json.loads(self.checkpoint().read_text())["parts"]), 1)
        retry = FakeSlides(self.home)
        retry.read()
        self.assertEqual(retry.reads, ["slide-2", "slide-3"])

    def test_failed_atomic_save_preserves_prior_prefix_and_discards_memory_only_slide(self):
        self.partial()
        previous = self.checkpoint().read_bytes()
        client = FakeSlides(self.home)
        with patch("tools.cha_philosophy.document_resume.os.replace", side_effect=OSError("private path details")):
            with self.assertRaisesRegex(ConnectorError, "^document_resume_write_failed$"):
                client.read()
        self.assertEqual(self.checkpoint().read_bytes(), previous)
        self.assertEqual(client.reads, ["slide-2"])
        self.assertFalse((self.checkpoint().parent / "checkpoint.tmp").exists())
        retry = FakeSlides(self.home)
        retry.read()
        self.assertEqual(retry.reads, ["slide-2", "slide-3"])

    def test_abandoned_atomic_temp_is_never_loaded_as_progress(self):
        self.partial()
        pending = self.checkpoint().parent / "checkpoint.tmp"
        pending.write_text('{"incomplete":')
        pending.chmod(0o600)
        retry = FakeSlides(self.home)
        retry.read()
        self.assertEqual(retry.reads, ["slide-2", "slide-3"])
        self.assertFalse(pending.exists())

    def test_revision_changed_since_checkpoint_restarts_all_slides(self):
        self.partial()
        client = FakeSlides(self.home)
        client.metadata["modifiedTime"] = "2026-09-13T01:00:00Z"
        result = client.read()
        self.assertEqual(client.reads, client.ids)
        self.assertEqual(result["extraction"]["resumed_slide_count"], 0)

    def test_reordered_inventory_with_same_timestamp_restarts_all_slides(self):
        self.partial()
        client = FakeSlides(self.home, ids=["slide-2", "slide-1", "slide-3"])
        client.read()
        self.assertEqual(client.reads, client.ids)

    def test_stale_caller_metadata_cannot_reuse_or_return_cached_text(self):
        self.partial()
        client = FakeSlides(self.home)
        client.metadata["modifiedTime"] = "changed"
        with self.assertRaisesRegex(ConnectorError, "^document_changed_during_read$"):
            client._read_native_slide_text(META, inventory())
        self.assertEqual(client.reads, [])
        self.assertFalse(self.checkpoint().exists())

    def test_fresh_inventory_mismatch_clears_old_cache_before_any_slide(self):
        self.partial()
        client = FakeSlides(self.home, ids=["slide-2", "slide-1", "slide-3"])
        with self.assertRaisesRegex(ConnectorError, "^document_slide_inventory_changed$"):
            client._read_native_slide_text(META, inventory())
        self.assertEqual(client.reads, [])
        self.assertFalse(self.checkpoint().exists())

    def test_final_order_change_or_access_loss_prevents_return_and_clears_cache(self):
        for final_order, final_error, expected in (
                (["slide-2", "slide-1", "slide-3"], None, "document_slide_inventory_changed"),
                (None, ConnectorError("source_access_denied"), "source_access_denied")):
            client = FakeSlides(self.home)
            client.final_inventory = final_order
            client.final_metadata_failure = final_error
            with self.assertRaisesRegex(ConnectorError, "^" + expected + "$"):
                client.read()
            self.assertFalse(self.checkpoint().exists())

    def test_complete_cached_prefix_still_requires_fresh_final_checks(self):
        client = FakeSlides(self.home)
        client.final_metadata_failure = ConnectorError("gog_read_failed")
        with self.assertRaisesRegex(ConnectorError, "^gog_read_failed$"):
            client.read()
        self.assertEqual(len(json.loads(self.checkpoint().read_text())["parts"]), 3)
        resumed = FakeSlides(self.home)
        result = resumed.read()
        self.assertEqual(resumed.reads, [])
        self.assertEqual(resumed.events, ["metadata", "inventory", "inventory", "metadata"])
        self.assertEqual(result["extraction"]["resumed_slide_count"], 3)

    def test_metadata_denied_before_resume_discards_old_checkpoint(self):
        self.partial()
        client = FakeSlides(self.home)
        client.metadata_failure = ConnectorError("source_not_found")
        with self.assertRaisesRegex(ConnectorError, "^source_not_found$"):
            client.read()
        self.assertEqual(client.reads, [])
        self.assertFalse(self.checkpoint().exists())

    def test_truncated_or_hash_modified_checkpoint_never_returns_or_reads_further(self):
        for damage in (lambda raw: raw[:len(raw)//2], lambda raw: raw.replace(b"body slide-1", b"fake slide-1")):
            self.partial()
            self.checkpoint().write_bytes(damage(self.checkpoint().read_bytes()))
            client = FakeSlides(self.home)
            with self.assertRaisesRegex(ConnectorError, "^document_resume_cache_invalid$"):
                client.read()
            self.assertEqual(client.reads, [])
            client.discard_document_cache("file-a")

    def test_oversized_checkpoint_is_rejected_before_json_read(self):
        self.partial()
        limit = self.cache().cache_budget
        with self.checkpoint().open("r+b") as handle:
            handle.truncate(limit + 1)
        with self.assertRaisesRegex(ConnectorError, "^document_resume_cache_budget_exceeded$"):
            FakeSlides(self.home).read()

    def test_existing_text_budget_includes_resumed_prefix(self):
        self.partial()
        client = FakeSlides(self.home)
        with patch("tools.cha_philosophy.connectors._DOCUMENT_BYTES", 60):
            with self.assertRaisesRegex(ConnectorError, "^document_text_budget_exceeded$"):
                client.read()
        self.assertEqual(len(json.loads(self.checkpoint().read_text())["parts"]), 1)

    def test_account_namespaces_and_discard_are_isolated(self):
        self.partial(account="account-a")
        self.partial(account="account-b")
        second_before = self.checkpoint("account-b").read_bytes()
        self.assertEqual(FakeSlides(self.home).discard_document_cache("file-a"), {"state": "discarded"})
        self.assertFalse(self.checkpoint().exists())
        self.assertEqual(self.checkpoint("account-b").read_bytes(), second_before)

    def test_concurrent_reader_is_rejected_and_access_loss_marks_running_reader(self):
        self.partial()
        with self.cache() as active:
            with self.assertRaisesRegex(ConnectorError, "^document_resume_busy$"):
                FakeSlides(self.home).read()
            self.assertEqual(FakeSlides(self.home).discard_document_cache("file-a"), {"state": "discard_pending"})
            with self.assertRaisesRegex(ResumeError, "^document_resume_invalidated$"):
                active.finish()
            self.assertFalse(self.checkpoint().exists())

    def test_mid_read_invalidation_never_registers_or_returns_completed_text(self):
        client = FakeSlides(self.home)
        def invalidate(sid):
            if sid == "slide-2":
                self.assertEqual(FakeSlides(self.home).discard_document_cache("file-a"), {"state": "discard_pending"})
        client.on_slide = invalidate
        with self.assertRaisesRegex(ConnectorError, "^document_resume_invalidated$"):
            client.read()
        self.assertFalse(self.checkpoint().exists())

    def test_finish_cleanup_does_not_erase_concurrent_access_loss(self):
        self.partial()
        with self.cache() as active:
            original = active._unlink
            invalidations = []
            def during_cleanup(name):
                if name == "checkpoint.json" and not invalidations:
                    invalidations.append(FakeSlides(self.home).discard_document_cache("file-a"))
                return original(name)
            with patch.object(active, "_unlink", side_effect=during_cleanup):
                with self.assertRaisesRegex(ResumeError, "^document_resume_invalidated$"):
                    active.finish()
            self.assertEqual(invalidations, [{"state": "discard_pending"}])
            self.assertFalse(self.checkpoint().exists())

    def test_revision_cleanup_preserves_new_discard_until_next_failure_check(self):
        self.partial()
        old_binding = json.loads(self.checkpoint().read_text())["binding"]
        binding = {k: v for k, v in old_binding.items() if k not in {"account", "file_id"}}
        binding["modified_time"] = "new-revision"
        with self.cache() as active:
            original = active._unlink
            invalidations = []
            def during_cleanup(name):
                if name == "checkpoint.json" and not invalidations:
                    invalidations.append(FakeSlides(self.home).discard_document_cache("file-a"))
                return original(name)
            with patch.object(active, "_unlink", side_effect=during_cleanup):
                self.assertEqual(active.load(binding), [])
            self.assertTrue((active.path / "discard.requested").exists())
            with self.assertRaisesRegex(ResumeError, "^document_resume_invalidated$"):
                active.append("slide-1", "fresh text")

    def test_access_loss_before_fallback_entry_also_discards_previous_progress(self):
        for operation in (("slides", "list-slides"), ("drive", "download")):
            for error in ("source_access_denied", "source_not_found"):
                self.partial()
                client = FakeSlides(self.home)
                original = client._call
                def call(command, args, **kwargs):
                    if command == operation:
                        raise ConnectorError(error)
                    return original(command, args, **kwargs)
                with patch.object(client, "_call", side_effect=call):
                    with self.assertRaisesRegex(ConnectorError, "^" + error + "$"):
                        client.read_document(META)
                self.assertFalse(self.checkpoint().exists())
                self.assertEqual(client.reads, [])

    def test_pending_discard_survives_reader_exit_and_forces_full_restart(self):
        self.partial()
        with self.cache():
            FakeSlides(self.home).discard_document_cache("file-a")
        client = FakeSlides(self.home)
        result = client.read()
        self.assertEqual(client.reads, client.ids)
        self.assertEqual(result["extraction"]["resumed_slide_count"], 0)

    def test_foreign_files_and_links_are_rejected_without_following_or_removing(self):
        self.partial()
        unknown = self.checkpoint().parent / "user-document.txt"
        unknown.write_text("preserve this file")
        unknown.chmod(0o600)
        with self.assertRaisesRegex(ConnectorError, "^document_resume_cache_unsafe$"):
            FakeSlides(self.home).discard_document_cache("file-a")
        self.assertEqual(unknown.read_text(), "preserve this file")
        unknown.unlink()
        outside = self.home / "outside"
        outside.write_text("preserve outside")
        outside.chmod(0o600)
        self.checkpoint().unlink()
        self.checkpoint().symlink_to(outside)
        with self.assertRaisesRegex(ConnectorError, "^document_resume_cache_unsafe$"):
            FakeSlides(self.home).read()
        self.checkpoint().unlink()
        os.link(outside, self.checkpoint())
        with self.assertRaisesRegex(ConnectorError, "^document_resume_cache_unsafe$"):
            FakeSlides(self.home).read()
        self.assertEqual(outside.read_text(), "preserve outside")

    def test_nonprivate_home_is_rejected_without_chmod(self):
        self.home.chmod(0o755)
        with self.assertRaisesRegex(ConnectorError, "^document_resume_cache_unsafe$"):
            FakeSlides(self.home).read()
        self.assertEqual(self.home.stat().st_mode & 0o777, 0o755)


if __name__ == "__main__":
    unittest.main()
