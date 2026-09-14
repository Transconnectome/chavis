"""File-local Drive work must not materialize or modify neighboring sources."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from tools.cha_philosophy.connectors import (
    ConnectorError, normalize_drive_comment, normalize_drive_document,
)
from tools.cha_philosophy.store import Store, digest
from tools.cha_philosophy.sync import refresh_supported_drive_evidence, sync_drive


CONFIG = {"account": "professor@example.invalid", "drive_account_is_professor": True,
          "drive_query": "synthetic inventory"}


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "private")
    yield value
    value.close()


def metadata(fid="target", account=CONFIG["account"]):
    return {"id": fid, "name": "Synthetic document", "_account_id": account,
            "mimeType": "application/vnd.google-apps.document",
            "modifiedTime": "2026-01-01T00:00:00Z"}


def seed(store, fid="target", account=CONFIG["account"], *, inventory=None, inline=False):
    meta = metadata(fid, account)
    body = normalize_drive_document(meta, {"extracted_text": "Original body.",
                                          "extraction": {"format": "synthetic"}})
    comment = normalize_drive_comment(meta, {
        "id": "comment", "author": {"me": True},
        "content": "Keep claims within the evidence.", "replies": [],
        "createdTime": "2026-01-01T00:00:00Z",
    }, True)[0]
    rows = [body, comment]
    if inline:
        extra = deepcopy(body)
        extra["source_id"] += "/synthetic-inline"
        extra["body"] = "Prior proposed wording."
        extra["metadata"]["view_role"] = "suggestions_inline_context"
        rows.append(extra)
    for row in rows:
        if inventory:
            row["metadata"]["sync_scope"] = inventory
        store.upsert(row, verified_remote=True)
    return [store.get_source(row["source_id"]) for row in rows]


def block_reads(store, monkeypatch, source_ids):
    original = store.get_source
    blocked = set(source_ids)

    def guarded(source_id):
        assert source_id not in blocked, "unrelated source was materialized"
        return original(source_id)

    monkeypatch.setattr(store, "get_source", guarded)
    return original


def test_sources_scope_filters_before_loading_and_preserves_default_filters(store, monkeypatch):
    target = seed(store)
    neighbor = seed(store, "target-extra")
    foreign_platform = deepcopy(target[1])
    foreign_platform.update(source_id="synthetic-gmail", platform="gmail")
    store.upsert(foreign_platform, verified_remote=True)
    inactive = deepcopy(target[0])
    inactive.update(source_id="synthetic-inactive", status="unavailable")
    store.upsert(inactive)
    original_all = list(store.sources())
    expected_drive = [row for row in original_all if row["platform"] == "drive"]
    assert list(store.sources(platform="drive")) == expected_drive
    assert [row["source_id"] for row in original_all] == sorted(
        row["source_id"] for row in original_all)

    block_reads(store, monkeypatch, [r["source_id"] for r in neighbor] + [inactive["source_id"]])
    for platform, direct in [("drive", False), ("drive", True), (None, False)]:
        expected = [row for row in original_all if row["scope"] == "drive:target"
                    and (platform is None or row["platform"] == platform)
                    and (not direct or row["authorship"] == "direct")]
        assert list(store.sources(platform=platform, direct=direct, scope="drive:target")) == expected
    # A specified empty/nonliteral scope must not broaden to all files or act as SQL.
    for scope in ("", "drive:target%", "drive:target' OR 1=1 --"):
        assert list(store.sources(platform="drive", scope=scope)) == []


class Client:
    account = CONFIG["account"]

    def __init__(self, mode="normal", *, complete_inventory=False):
        self.mode = mode
        self.complete_inventory = complete_inventory
        self.discarded = []

    def drive_page(self, query, page_token=""):
        return {"files": [{"id": "target"}] + (
            [] if self.complete_inventory else [{"id": "pending-next"}])}

    def get_drive_metadata(self, fid):
        assert fid == "target"
        if self.mode == "metadata_denied":
            raise ConnectorError("source_access_denied")
        return {**metadata(fid), "modifiedTime": "2026-01-02T00:00:00Z",
                "trashed": self.mode == "trashed"}

    def read_document(self, meta):
        if self.mode == "final_metadata_denied":
            exc = ConnectorError("source_not_found")
            exc.file_access_withdrawn = True
            raise exc
        if self.mode == "body_gap":
            raise ConnectorError("document_native_doc_timeout")
        return {"extracted_text": "Updated canonical body.",
                "extraction": {"format": "synthetic"}}

    def iter_drive_comments(self, fid, *, progress=None):
        if self.mode == "comments_denied":
            raise ConnectorError("source_access_denied")
        progress.complete = True
        return iter(())

    def discard_document_cache(self, fid):
        self.discarded.append(fid)


@pytest.mark.parametrize("mode", ["normal", "metadata_denied", "trashed",
                                 "final_metadata_denied", "body_gap", "comments_denied"])
def test_file_local_sync_does_not_read_neighbors_and_preserves_reconciliation(store, monkeypatch, mode):
    body, comment, inline = seed(store, inline=True)
    neighbors = seed(store, "target-extra")
    original_get = block_reads(store, monkeypatch, [row["source_id"] for row in neighbors])
    client = Client(mode)
    result = sync_drive(store, CONFIG, client, 1)
    assert result["processed_files"] == result["pending_files"] == 1
    assert result["inventory_complete"] is False
    assert all(original_get(row["source_id"]) == row for row in neighbors)
    after_body, after_comment, after_inline = [original_get(row["source_id"])
                                             for row in (body, comment, inline)]
    assert after_comment["status"] == "unavailable"
    if mode in {"metadata_denied", "trashed", "final_metadata_denied"}:
        assert after_body["status"] == after_inline["status"] == "unavailable"
        assert after_body["verified_at"] == body["verified_at"]
        assert client.discarded == ["target"]
    elif mode == "body_gap":
        assert after_body == body and after_inline == inline
    else:
        assert after_body["body"] == "Updated canonical body."
        assert after_inline["body"] == inline["body"]
        assert after_inline["verified_at"] == inline["verified_at"]
        assert after_inline["metadata"]["paired_view_status"] == "stale_for_canonical_revision"


@pytest.mark.parametrize("stage", ["metadata", "comments"])
def test_supported_refresh_keeps_other_files_and_other_accounts(store, monkeypatch, stage):
    body, comment = seed(store)
    neighbors = seed(store, "target-extra")
    other_account = seed(store, account="other@example.invalid")
    pid = store.add_principle({
        "statement": "Claims require evidence.", "domains": ["review"],
        "evidence": [{"source_id": comment["source_id"], "source_hash": comment["source_hash"],
                      "quote": comment["authored_text"]}],
    })
    store.review(pid, "evidence_supported", "synthetic-reviewer", "Synthetic source review.")
    with store.transaction():
        store.db.execute("UPDATE sources SET verified_at=? WHERE source_id=?", (
            (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(), comment["source_id"]))
    original_get = block_reads(store, monkeypatch, [row["source_id"] for row in neighbors])
    result = refresh_supported_drive_evidence(store, CONFIG, Client(stage + "_denied"))
    assert result["withdrawn_sources"] == (2 if stage == "metadata" else 1)
    assert not result["failures"]
    assert original_get(comment["source_id"])["status"] == "unavailable"
    if stage == "comments":
        assert original_get(body["source_id"]) == body
    else:
        assert original_get(body["source_id"])["status"] == "unavailable"
    assert all(original_get(row["source_id"]) == row for row in neighbors + other_account)


def test_complete_inventory_still_reconciles_missing_files_in_its_query(store):
    inventory = "drive:" + CONFIG["account"] + ":" + digest(CONFIG["drive_query"])[:12]
    missing = seed(store, "missing-from-complete-query", inventory=inventory)
    other_inventory = seed(store, "outside-query", inventory="drive:other-query")
    result = sync_drive(store, CONFIG, Client(complete_inventory=True), 1)
    assert result["inventory_complete"] is True
    assert all(store.get_source(row["source_id"])["status"] == "unavailable" for row in missing)
    assert all(store.get_source(row["source_id"]) == row for row in other_inventory)
