"""Two Docs views remain separate context; failures preserve prior verification."""
from copy import deepcopy
import json
import subprocess
from unittest.mock import Mock

import pytest

from tools.cha_philosophy import connectors as c
from tools.cha_philosophy.docs_text import BASE_LIMITATIONS, VIEW_MODES
from tools.cha_philosophy.store import Store
from tools.cha_philosophy.sync import sync, sync_drive


ACCOUNT = "professor@example.invalid"
BASE = "/synthetic/gog-installed"
ALTERNATE = "/synthetic/gog-structured-docs"
META = {"id": "doc", "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-09-13T00:00:00Z", "trashed": False,
        "_account_id": ACCOUNT, "name": "Synthetic shared manuscript"}
CONFIG = {"account": ACCOUNT, "drive_file_ids": ["doc"], "drive_query": "synthetic query",
          "drive_account_is_professor": True}


def record(text, content=0, element=0, **changes):
    return {"tabId": "t.1", "region": "body", "regionId": "", "kind": "text", "text": text,
            "path": ["content", str(content), "elements", str(element)], "startIndex": 0, "endIndex": 30,
            "suggestedInsertionIds": [], "suggestedDeletionIds": [], **changes}


def recount(data):
    for view in data["views"]:
        view["textBytes"] = sum(len(r["text"].encode()) for r in view["records"])
    data["textBytes"] = (len(data["title"].encode()) + sum(len(t["title"].encode()) for t in data["tabs"])
                         + sum(v["textBytes"] for v in data["views"]))
    return data


def payload():
    base = [record("Correlation ", endIndex=12), record("r=0.42.😊\n", element=1, startIndex=12, endIndex=22),
            record("Base cell.\n", path=["content", "1", "tableRows", "0", "tableCells", "0", "content", "0", "elements", "0"]),
            record("Before", content=2), record("", content=2, element=1, kind="unsupported_equation"),
            record("After\n", content=2, element=2),
            record("", content=3, kind="footnote_reference", footnoteId="f.1", footnoteNumber=""),
            record("Header text.\n", region="header", regionId="h.1"),
            record("Footnote text.\n", region="footnote", regionId="f.1"),
            record("Child tab text.\n", tabId="t.2")]
    inline = deepcopy(base)
    inline[2]["text"] = "Old cell. "
    inline[2]["suggestedDeletionIds"] = ["s.cell", "s.row", "s.table"]
    proposed = deepcopy(inline[2])
    proposed["path"][-1] = "1"
    proposed.update(text="New proposed cell.\n", suggestedDeletionIds=[], suggestedInsertionIds=["s.cell", "s.row", "s.table"])
    inline.insert(3, proposed)
    return recount({"schemaVersion": 1, "documentId": "doc", "title": "Research document", "revisionId": "revision-1",
                    "tabCount": 2, "tabs": [{"tabId": "t.1", "title": "Main", "parentTabId": "", "index": 0, "nestingLevel": 0},
                                             {"tabId": "t.2", "title": "Child", "parentTabId": "t.1", "index": 0, "nestingLevel": 1}],
                    "views": [{"mode": VIEW_MODES[0], "records": base}, {"mode": VIEW_MODES[1], "records": inline}],
                    "revisionAndTabsRechecked": True, "tabHierarchyRechecked": True, "partialText": True,
                    "limitations": sorted(BASE_LIMITATIONS)})


def client_for(data=None, *, failure=None, final_metadata=None, final_failure=None):
    data = payload() if data is None else data
    calls = []
    document_called = False

    def runner(argv, **kwargs):
        nonlocal document_called
        calls.append((argv, kwargs))
        expected = {"shell": False, "capture_output": True, "text": True, "check": False,
                    "timeout": 270 if argv[0] == ALTERNATE else 90}
        assert kwargs == expected
        if argv[0] == ALTERNATE:
            document_called = True
            assert argv == [ALTERNATE, "--account=" + ACCOUNT, "--no-input", "--json", "docs",
                            "read-document-text", "doc", "--max-bytes=33554432"]
            if failure:
                return subprocess.CompletedProcess(argv, 1, "", failure + " SYNTHETIC_PRIVATE_STDERR")
            return subprocess.CompletedProcess(argv, 0, data if isinstance(data, str) else json.dumps(data), "")
        assert argv == [BASE, "--account=" + ACCOUNT, "--json", "--no-input", "drive", "get", "doc"]
        if document_called and final_failure:
            return subprocess.CompletedProcess(argv, 1, "", final_failure + " SYNTHETIC_PRIVATE_STDERR")
        return subprocess.CompletedProcess(argv, 0, json.dumps({"file": (final_metadata if document_called else None) or META}), "")

    client = c.GogClient(ACCOUNT, executable=BASE, docs_text_executable=ALTERNATE, runner=runner)
    client.drive_page = lambda query, page: {"files": []}

    def comments(file_id, *, progress):
        progress.complete = True
        return iter(())

    client.iter_drive_comments = comments
    return client, calls


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "private")
    yield value
    value.close()


def read_and_sync(store, **kwargs):
    client, calls = client_for(**kwargs)
    result = sync_drive(store, CONFIG, client, 2)
    rows = {s["metadata"]["view_role"]: s for s in store.sources(platform="drive")}
    return result, rows, calls


def test_two_views_are_separate_linked_context_and_one_file(store):
    result, rows, calls = read_and_sync(store)
    base, inline = rows["base_without_suggestions"], rows["suggestions_inline_context"]
    assert result["processed_files"] == result["cycle_files"] == 1
    assert result["changed_sources"] == 2 and len(rows) == 2
    assert len(calls) == 3  # Initial Drive metadata, one Docs command, final Drive metadata.
    assert "Correlation r=0.42.😊\n" in base["body"]
    assert "Before[not extracted: unsupported_equation]After" in base["body"]
    assert "New proposed cell" not in base["body"] and "Base cell." in base["body"]
    assert "New proposed cell" in inline["body"] and "Old cell." in inline["body"]
    assert "SUGGESTIONS_INLINE" in inline["body"]
    assert "insertion=s.cell,s.row,s.table" in inline["body"]
    assert "deletion=s.cell,s.row,s.table" in inline["body"]
    assert inline["metadata"]["canonical_document_source_id"] == base["source_id"]
    assert inline["source_id"] != base["source_id"]
    for row in rows.values():
        assert row["authorship"] == "unverified" and row["authored_text"] == ""
        assert row["metadata"]["source_kind"] == "document"
        assert row["metadata"]["alternative_view_not_independent_evidence"] is True
        assert row["metadata"]["paired_view_status"] == "verified_together"
        extraction = row["metadata"]["extraction"]
        assert extraction["index_units"] == "google_docs_utf16_code_units"
        assert extraction["indices_are_view_specific"] and not extraction["indices_are_source_body_spans"]
        assert extraction["drive_metadata_rechecked"] and extraction["partial_text"]
        assert any(r["kind"] == "footnote_reference" and r["footnoteNumber"] == "" for r in extraction["view_records"])
        assert any(r["kind"] == "unsupported_equation" for r in extraction["view_records"])
        assert store.validate_evidence([{"source_id": row["source_id"], "source_hash": row["source_hash"], "quote": "New proposed cell"}])
    assert list(store.pending_extraction()) == []
    assert store.search("proposed") == []
    assert [s["source_id"] for s in store.search("proposed", direct=False)] == [inline["source_id"]]


def test_proposal_only_document_has_empty_base_and_preserved_inline(store):
    data = payload()
    data["views"][0]["records"] = []
    data["views"][1]["records"] = [record("Pending proposal only.", suggestedInsertionIds=["s.table"])]
    _, rows, _ = read_and_sync(store, data=recount(data))
    assert rows["base_without_suggestions"]["body"] == ""
    assert "Pending proposal only." in rows["suggestions_inline_context"]["body"]
    assert rows["base_without_suggestions"]["metadata"]["extraction"]["view_text_bytes"] == 0


def test_credentials_are_sanitized_in_both_bodies_and_nested_records():
    data = payload()
    for view in data["views"]:
        view["records"][0]["text"] = "Passcode: SYNTHETIC_MEETING_SECRET\n"
    client, _ = client_for(recount(data))
    document = client.read_document(META)
    assert "SYNTHETIC_MEETING_SECRET" not in json.dumps(document)
    assert "r=0.42" in document["extracted_text"]


def test_two_view_upsert_rolls_back_as_one_unit(store, monkeypatch):
    original = store.upsert

    def fail_second(source, **kwargs):
        if source.get("metadata", {}).get("view_role") == "suggestions_inline_context":
            raise RuntimeError("synthetic second write failure")
        return original(source, **kwargs)

    monkeypatch.setattr(store, "upsert", fail_second)
    client, _ = client_for()
    with pytest.raises(RuntimeError):
        sync_drive(store, CONFIG, client, 2)
    assert list(store.sources(platform="drive")) == []
    assert store.db.execute("SELECT count(*) FROM audit WHERE action='source_upsert'").fetchone()[0] == 0


@pytest.mark.parametrize("changed", [False, True])
def test_default_route_retains_old_inline_with_original_verification_and_explicit_link_state(store, changed):
    _, rows, _ = read_and_sync(store)
    before = rows["suggestions_inline_context"]
    client, _ = client_for()
    current_meta = {**META, "modifiedTime": "2026-09-13T01:00:00Z"} if changed else META
    client.get_drive_metadata = lambda fid: current_meta
    client.read_document = lambda meta: {"extracted_text": "A default route body.", "extraction": {"format": "google_docs"}}
    result = sync_drive(store, CONFIG, client, 2)
    after = store.get_source(before["source_id"])
    assert result["processed_files"] == 1
    assert after["status"] == "active" and after["body"] == before["body"]
    assert after["source_hash"] == before["source_hash"] and after["verified_at"] == before["verified_at"]
    assert after["modified_at"] == before["modified_at"]
    expected = "stale_for_canonical_revision" if changed else "not_rechecked_with_canonical"
    assert after["metadata"]["paired_view_status"] == expected


@pytest.mark.parametrize("stage", ["preview", "inline", "verification"])
def test_docs_stage_denial_preserves_prior_views_and_verification(store, stage):
    _, rows, _ = read_and_sync(store)
    client, calls = client_for(failure="docs_text_" + stage + "_access_denied")
    result = sync_drive(store, CONFIG, client, 2)
    assert result["document_read_gaps"][0]["reason"] == "document_docs_" + stage + "_access_denied"
    assert result["processed_files"] == 1 and len(calls) == 2
    assert result["access_withdrawn"] == 0
    assert all(store.get_source(src["source_id"]) == src for src in rows.values())


@pytest.mark.parametrize("failure", ["403 forbidden", "404 notfound", "trashed"])
def test_final_drive_withdrawal_revokes_both_views(store, failure):
    _, rows, _ = read_and_sync(store)
    client, _ = (client_for(final_metadata={**META, "trashed": True}) if failure == "trashed"
                 else client_for(final_failure=failure))
    result = sync_drive(store, CONFIG, client, 2)
    assert result["access_withdrawn"] == 1
    assert result["document_read_gaps"][0]["stage"] == "final_metadata"
    for old in rows.values():
        src = store.get_source(old["source_id"])
        assert src["status"] == "unavailable" and src["body"] == "" and src["metadata"] == {}


@pytest.mark.parametrize("change", [{"id": "different"}, {"mimeType": "text/plain"},
                                     {"modifiedTime": "2026-09-13T01:00:00Z"}])
def test_final_metadata_mismatch_preserves_both_old_views_and_timestamps(store, change):
    _, rows, _ = read_and_sync(store)
    client, calls = client_for(final_metadata={**META, **change})
    result = sync_drive(store, CONFIG, client, 2)
    assert result["document_read_gaps"][0]["reason"] == "document_changed_during_read"
    assert result["access_withdrawn"] == 0 and len(calls) == 3
    assert all(store.get_source(src["source_id"]) == src for src in rows.values())


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(schemaVersion=True),
    lambda p: p.update(documentId="another"),
    lambda p: p.update(unexpected="private raw field"),
    lambda p: p.update(tabCount=1),
    lambda p: p["tabs"][1].update(tabId="t.1"),
    lambda p: p["tabs"][1].update(parentTabId="absent"),
    lambda p: p["tabs"][1].update(index=1),
    lambda p: p["tabs"][1].update(nestingLevel=2),
    lambda p: p["views"].reverse(),
    lambda p: p["views"].pop(),
    lambda p: p["views"][0].update(textBytes=0),
    lambda p: p["views"][0]["records"].reverse(),
    lambda p: p["views"][0]["records"].insert(1, deepcopy(p["views"][0]["records"][0])),
    lambda p: p["views"][0]["records"][0].update(tabId="absent"),
    lambda p: p["views"][0]["records"][0].update(path=["content", "0", "tableRows", "0"]),
    lambda p: p["views"][0]["records"][0].update(startIndex=True),
    lambda p: p["views"][0]["records"][0].update(startIndex=100),
    lambda p: p["views"][0]["records"][0].update(suggestedInsertionIds=["z", "a"]),
    lambda p: p["views"][0]["records"][0].update(suggestedInsertionIds=["a", "a"]),
    lambda p: p["views"][0]["records"][0].update(kind=["text"]),
    lambda p: p["views"][0]["records"][0].update(displayFallback="email"),
    lambda p: p["views"][0]["records"][4].update(text="invented equation"),
    lambda p: p["views"][0]["records"][6].pop("footnoteNumber"),
    lambda p: p.update(revisionAndTabsRechecked=False),
    lambda p: p.update(revisionId=""),
    lambda p: p.update(revisionId="", revisionAndTabsRechecked=False),
    lambda p: p.update(tabHierarchyRechecked=False),
    lambda p: p.update(partialText=False),
    lambda p: p["limitations"].append("unknown_private_string"),
    lambda p: p.update(textBytes=p["textBytes"] + 1),
])
def test_invalid_view_structure_or_accounting_never_falls_back(mutate):
    data = payload()
    mutate(data)
    client, calls = client_for(data)
    with pytest.raises(c.ConnectorError, match="document_docs_"):
        client.read_document(META)
    assert len(calls) == 1


def test_missing_revision_requires_explicit_limitation_and_final_drive_check():
    data = payload()
    data.update(revisionId="", revisionAndTabsRechecked=False)
    data["limitations"].append("revision_id_unavailable")
    client, calls = client_for(data)
    document = client.read_document(META)
    assert len(calls) == 2
    for view in [document, *document["context_views"]]:
        assert view["extraction"]["docs_revision_and_tabs_rechecked"] is False
        assert view["extraction"]["drive_metadata_rechecked"] is True


@pytest.mark.parametrize("marker,code", [
    ("auth_required", "gog_auth_required"), ("rate_limited", "gog_rate_limited"),
    ("quota_exceeded", "gog_quota_exceeded"), ("read_failed", "gog_read_failed"),
    ("changed_during_read", "document_changed_during_read"),
    ("byte_budget_exceeded", "document_response_budget_exceeded"),
    ("tab_depth_exceeded", "document_docs_tab_depth_exceeded"),
    ("content_depth_exceeded", "document_docs_content_depth_exceeded"),
    ("node_budget_exceeded", "document_docs_node_budget_exceeded"),
])
def test_fixed_failures_remain_source_free_and_do_not_fallback(marker, code):
    client, calls = client_for(failure="docs_text_" + marker)
    with pytest.raises(c.ConnectorError) as error:
        client.read_document(META)
    assert error.value.code == code and error.value.operation == "docs.read-document-text"
    assert "SYNTHETIC_PRIVATE" not in str(error.value)
    assert len(calls) == 1


@pytest.mark.parametrize("raw", ['{"schemaVersion":1,"schemaVersion":1}', '{"number":NaN}', 'not json'])
def test_duplicate_nonfinite_or_malformed_json_is_not_readable(raw):
    client, calls = client_for(raw)
    with pytest.raises(c.ConnectorError, match="gog_invalid_json"):
        client.read_document(META)
    assert len(calls) == 1


def test_output_budget_is_checked_before_decode():
    client, _ = client_for(" " * (c._DOCUMENT_BYTES + 1))
    with pytest.raises(c.ConnectorError, match="document_response_budget_exceeded"):
        client.read_document(META)


def test_only_selected_docs_command_uses_alternate_executable_and_long_timeout():
    runner = Mock(return_value=subprocess.CompletedProcess([], 0, '{"document":{"tabs":[]}}', ""))
    client = c.GogClient(ACCOUNT, executable=BASE, docs_text_executable=ALTERNATE, runner=runner)
    with pytest.raises(c.ConnectorError, match="gog_command_not_readonly"):
        client._call(("docs", "read-document-text"), ["doc"])
    client.get_drive_document("doc")
    assert runner.call_args.args[0][0] == BASE and runner.call_args.kwargs["timeout"] == 90


def test_sync_wires_both_opt_in_paths_without_replacing_default_binary(store, monkeypatch):
    factory = Mock(return_value=object())
    monkeypatch.setattr("tools.cha_philosophy.sync.GogClient", factory)
    monkeypatch.setattr("tools.cha_philosophy.sync.sync_drive", Mock(return_value={"platform": "drive", "state": "partial"}))
    config = {**CONFIG, "drive_docs_text_executable": ALTERNATE, "drive_slides_text_executable": "/synthetic/slides"}
    sync(store, config, "drive", 1)
    factory.assert_called_once_with(ACCOUNT, document_cache_home=store.home,
                                    slides_text_executable="/synthetic/slides", docs_text_executable=ALTERNATE)


def test_new_fixed_errors_survive_safe_status_without_arbitrary_text():
    from tools.cha_philosophy.refresh import _source_summary
    codes = set(c._DOCS_TEXT_ERRORS.values()) | {"document_docs_revision_required", "gog_docs_text_executable_invalid"}
    for code in codes:
        summary = _source_summary("drive", {"results": [{"platform": "drive", "state": "error", "error_code": code,
                                   "body": "SYNTHETIC_PRIVATE_SOURCE"}]})
        assert summary["error_code"] == code
        assert "SYNTHETIC_PRIVATE_SOURCE" not in json.dumps(summary)
