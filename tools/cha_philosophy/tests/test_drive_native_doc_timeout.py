"""Only the observed Docs header timeout becomes a per-document coverage gap."""
import json
import subprocess
from unittest.mock import Mock

import pytest

from tools.cha_philosophy.connectors import ConnectorError, GogClient
from tools.cha_philosophy.sync import sync_drive
from tools.cha_philosophy.tests.test_sync import CONFIG, DriveFixture, opened


HEADER_TIMEOUT = "net/http: timeout awaiting response headers"


def failing_gog(error=HEADER_TIMEOUT):
    return GogClient(CONFIG["account"], runner=Mock(return_value=
        subprocess.CompletedProcess([], 1, "", error)))


def test_confirmed_docs_header_timeout_has_safe_document_gap_code_and_operation():
    client = failing_gog(HEADER_TIMEOUT + " https://private.invalid/PRIVATE-ID?token=PRIVATE-TOKEN")
    with pytest.raises(ConnectorError) as raised:
        client.get_drive_document("synthetic-file")
    assert raised.value.code == str(raised.value) == "document_native_doc_timeout"
    assert raised.value.operation == "docs.cat"
    assert "PRIVATE" not in str(raised.value)


@pytest.mark.parametrize("stronger,expected", [
    ("invalid_grant", "gog_auth_required"),
    ("401 Unauthorized", "gog_auth_required"),
    ("429", "gog_rate_limited"),
    ("rateLimitExceeded", "gog_rate_limited"),
    ("quotaExceeded", "gog_quota_exceeded"),
    ("dailyLimitExceeded", "gog_quota_exceeded"),
    ("403 Forbidden", "source_access_denied"),
    ("insufficientFilePermissions", "source_access_denied"),
    ("404 Not Found", "source_not_found"),
])
def test_auth_rate_quota_and_access_errors_keep_precedence(stronger, expected):
    with pytest.raises(ConnectorError, match="^" + expected + "$"):
        failing_gog(stronger + " " + HEADER_TIMEOUT).get_drive_document("synthetic-file")


@pytest.mark.parametrize("error", [
    "connection reset by peer", "context deadline exceeded", "TLS handshake timeout",
    "503 Service Unavailable", "400 Bad Request", "unclassified failure",
])
def test_other_docs_errors_remain_retryable_source_errors(error):
    with pytest.raises(ConnectorError, match="^gog_read_failed$"):
        failing_gog(error).get_drive_document("synthetic-file")


@pytest.mark.parametrize("command,args", [
    (("drive", "get"), ["synthetic-file"]),
    (("drive", "comments", "list"), ["synthetic-file", "--max=100"]),
    (("slides", "read-slide"), ["synthetic-file", "synthetic-slide"]),
    (("sheets", "metadata"), ["synthetic-file"]),
])
def test_identical_timeout_on_other_operations_is_unchanged(command, args):
    with pytest.raises(ConnectorError, match="^gog_read_failed$"):
        failing_gog()._call(command, args)


def test_local_subprocess_timeout_does_not_become_a_document_gap():
    runner = Mock(side_effect=subprocess.TimeoutExpired(["gog", "docs", "cat"], 25))
    client = GogClient(CONFIG["account"], runner=runner)
    with pytest.raises(ConnectorError, match="^gog_execution_failed$"):
        client.get_drive_document("synthetic-file")


class DocumentTimeoutFixture(DriveFixture):
    def __init__(self, pages, *, error=HEADER_TIMEOUT, local_timeout=False):
        super().__init__(pages)
        self.comment_calls = []
        self.document_calls = []
        self.gog = failing_gog(error)
        if local_timeout:
            self.gog.runner = Mock(side_effect=subprocess.TimeoutExpired(["gog", "docs", "cat"], 25))

    def read_document(self, meta):
        self.document_calls.append(meta["id"])
        if meta["id"] == "slow-doc":
            return self.gog.read_document(meta)
        return super().read_document(meta)

    def iter_drive_comments(self, fid, *, progress=None):
        self.comment_calls.append(fid)
        yield from super().iter_drive_comments(fid, progress=progress)


def test_document_gap_preserves_old_body_verification_reads_comments_and_continues(tmp_path):
    pages = {"": {"files": [{"id": "slow-doc"}, {"id": "next-doc"}]}}
    with opened(tmp_path / "store") as store:
        sync_drive(store, CONFIG, DriveFixture({"": {"files": [{"id": "slow-doc"}]}}), 10)
        previous = list(store.sources(platform="drive"))
        old_body = next(row for row in previous if row["metadata"]["source_kind"] == "document")
        old_comment = next(row for row in previous if row["metadata"]["source_kind"] == "comment")
        store.db.execute("UPDATE sources SET verified_at=?", ("2025-01-01T00:00:00+00:00",))
        store.db.commit()
        old_body = store.get_source(old_body["source_id"])
        client = DocumentTimeoutFixture(pages)
        result = sync_drive(store, CONFIG, client, 10)
        current = store.get_source(old_body["source_id"])
        assert current["status"] == "active"
        assert current["body"] == old_body["body"]
        assert current["source_hash"] == old_body["source_hash"]
        assert current["verified_at"] == old_body["verified_at"]
        assert client.document_calls == ["slow-doc", "next-doc"]
        assert client.comment_calls == ["slow-doc", "next-doc"]
        comment = store.get_source(old_comment["source_id"])
        assert comment["status"] == "active" and comment["authorship"] == "direct"
        assert comment["verified_at"] != old_body["verified_at"]
        assert result["state"] == "complete_for_query_with_content_gaps"
        assert result["processed_files"] == 2 and result["pending_files"] == 0
        gap = result["document_read_gaps"][0]
        assert gap["file_id"] == "slow-doc" and gap["stage"] == "document"
        assert gap["reason"] == "document_native_doc_timeout" and gap["operation"] == "docs.cat"
        assert gap["retry_policy"] == "next_full_inventory_cycle"
        assert any(row["metadata"]["file_id"] == "next-doc" for row in store.sources(platform="drive"))
        # The next full inventory really retries the Doc and clears a recovered gap.
        retried = sync_drive(store, CONFIG, DriveFixture(pages), 10)
        assert retried["state"] == "complete_for_query" and not retried["document_read_gaps"]
        assert store.get_source(old_body["source_id"])["verified_at"] != old_body["verified_at"]


@pytest.mark.parametrize("comment_error", [None, "source_access_denied"])
def test_document_timeout_does_not_keep_missing_or_inaccessible_comments_active(tmp_path, comment_error):
    class CommentsUnavailable(DocumentTimeoutFixture):
        def iter_drive_comments(self, fid, *, progress=None):
            if comment_error:
                raise ConnectorError(comment_error)
            progress.complete = True
            yield from ()
    pages = {"": {"files": [{"id": "slow-doc"}]}}
    with opened(tmp_path / "store") as store:
        sync_drive(store, CONFIG, DriveFixture(pages), 10)
        previous = list(store.sources(platform="drive"))
        body = next(row for row in previous if row["metadata"]["source_kind"] == "document")
        comment = next(row for row in previous if row["metadata"]["source_kind"] == "comment")
        result = sync_drive(store, CONFIG, CommentsUnavailable(pages), 10)
        current_body = store.get_source(body["source_id"])
        current_comment = store.get_source(comment["source_id"])
        assert current_body["status"] == "active" and current_body["body"] == body["body"]
        assert current_body["verified_at"] == body["verified_at"]
        assert current_comment["status"] == "unavailable" and current_comment["body"] == ""
        assert result["document_read_gaps"][0]["reason"] == "document_native_doc_timeout"


@pytest.mark.parametrize("error,local_timeout,expected", [
    ("connection reset by peer", False, "gog_read_failed"),
    ("invalid_grant " + HEADER_TIMEOUT, False, "gog_auth_required"),
    ("429 " + HEADER_TIMEOUT, False, "gog_rate_limited"),
    ("quotaExceeded " + HEADER_TIMEOUT, False, "gog_quota_exceeded"),
    (HEADER_TIMEOUT, True, "gog_execution_failed"),
])
def test_retryable_failures_preserve_pending_file_and_do_not_skip_to_comments(tmp_path, error, local_timeout, expected):
    client = DocumentTimeoutFixture({"": {"files": [{"id": "slow-doc"}, {"id": "next-doc"}]}},
                                   error=error, local_timeout=local_timeout)
    with opened(tmp_path / "store") as store:
        with pytest.raises(ConnectorError, match="^" + expected + "$"):
            sync_drive(store, CONFIG, client, 10)
        checkpoint = json.loads(store.db.execute("SELECT data FROM checkpoints WHERE scope LIKE 'drive:%'").fetchone()[0])
        assert checkpoint["pending"] == ["slow-doc", "next-doc"]
        assert checkpoint["files"] == 0 and not checkpoint["content_gaps"]
        assert client.comment_calls == []
        assert list(store.sources(platform="drive")) == []
