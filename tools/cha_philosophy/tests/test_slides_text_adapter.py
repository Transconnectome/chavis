"""The opt-in presentation reader is bounded, validated and never a fallback."""
from copy import deepcopy
import hashlib
import json
import subprocess
from unittest.mock import Mock

import pytest

from tools.cha_philosophy import connectors as c
from tools.cha_philosophy.document_resume import NativeSlidesResume
from tools.cha_philosophy.store import Store
from tools.cha_philosophy.sync import sync


ACCOUNT = "professor@example.invalid"
BASE = "/synthetic/gog-installed"
ALTERNATE = "/synthetic/gog-structured-slides"
META = {"id": "deck", "mimeType": "application/vnd.google-apps.presentation",
        "modifiedTime": "2026-09-13T00:00:00Z", "trashed": False,
        "_account_id": ACCOUNT, "name": "Synthetic research slides"}


def record(object_id, text, **kwargs):
    return {"objectId": object_id, "kind": "shape", "groupPath": [], "text": text, **kwargs}


def payload():
    result = {"schemaVersion": 1, "presentationId": "deck", "title": "Research title", "revisionId": "revision-1",
              "slideCount": 2, "slideOrderRechecked": True, "revisionAndOrderRechecked": True,
              "partialText": True, "limitations": sorted(c._SLIDES_TEXT_LIMITATIONS),
              "slides": [{"objectId": "slide:second", "number": 1,
                          "elements": [record("shape:1", "Group finding: r=0.42, p=0.03.\n", groupPath=["group:1"]),
                                       record("table:1", "N=148", kind="table_cell", rowIndex=0, cellIndex=0),
                                       record("table:1", "", kind="table_cell", rowIndex=0, cellIndex=1)],
                          "notes": [record("notes:body", "Speaker notes body.\n", placeholderType="BODY"),
                                    record("notes:other", "Non-body note annotation.", groupPath=["notes:group"])]},
                         {"objectId": "slide:first", "number": 2,
                          "elements": [record("shape:2", "Later slide text.")], "notes": []}]}
    result["textBytes"] = len(result["title"].encode()) + sum(
        len(r["text"].encode()) for page in result["slides"] for part in ("elements", "notes") for r in page[part])
    return result


def client_for(data=None, *, final_metadata=None, failure=None, cache_home=None):
    data = payload() if data is None else data
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        assert kwargs == {"shell": False, "capture_output": True, "text": True, "timeout": 90, "check": False}
        if argv[0] == ALTERNATE:
            assert argv == [ALTERNATE, "--account=" + ACCOUNT, "--no-input", "--json", "slides",
                            "read-presentation-text", "deck", "--max-bytes=33554432"]
            if failure:
                return subprocess.CompletedProcess(argv, 1, "", failure + " SYNTHETIC_PRIVATE_STDERR")
            return subprocess.CompletedProcess(argv, 0, data if isinstance(data, str) else json.dumps(data), "")
        assert argv == [BASE, "--account=" + ACCOUNT, "--json", "--no-input", "drive", "get", "deck"]
        return subprocess.CompletedProcess(argv, 0, json.dumps({"file": final_metadata or META}), "")

    return c.GogClient(ACCOUNT, executable=BASE, slides_text_executable=ALTERNATE,
                       runner=runner, document_cache_home=cache_home), calls


def incomplete_cache(home):
    home.chmod(0o700)
    ids = ["slide:second", "slide:first"]
    cache = NativeSlidesResume(home, ACCOUNT, "deck", text_budget=c._DOCUMENT_BYTES)
    with cache:
        cache.load({"mime_type": META["mimeType"], "modified_time": META["modifiedTime"], "slide_ids": ids,
                    "slide_order_sha256": hashlib.sha256(json.dumps(ids, separators=(",", ":")).encode()).hexdigest()})
        cache.append(ids[0], "Old incomplete top-level text.")
    return cache.path / "checkpoint.json"


def test_opt_in_preserves_groups_table_positions_and_all_notes_then_discards_cache(tmp_path):
    cache = incomplete_cache(tmp_path)
    client, calls = client_for(cache_home=tmp_path)
    document = client.read_document(META)
    text, extraction = document["extracted_text"], document["extraction"]
    assert len(calls) == 2  # One new command plus the independent final Drive read.
    assert "Group finding: r=0.42, p=0.03.\n" in text
    assert "[shape shape:1 groups group:1]" in text
    assert "[table table:1 row 0 cell 0]\nN=148" in text
    assert "[table table:1 row 0 cell 1]\n" in text
    assert "Speaker notes body.\n" in text and "Non-body note annotation." in text
    assert text.index("Group finding") < text.index("Later slide text.")
    assert extraction["native_slide_ids"] == ["slide:second", "slide:first"]
    assert extraction["partial_text"] is True
    assert extraction["slides_revision_and_order_rechecked"] is True
    assert extraction["drive_metadata_rechecked"] is True
    assert set(extraction["limitations"]) == c._SLIDES_TEXT_LIMITATIONS
    assert not cache.exists()
    assert "Old incomplete top-level text" not in text


def test_output_remains_unverified_authorship_and_meeting_credentials_are_sanitized():
    data = payload()
    data["slides"][0]["notes"].append(record("notes:secret", "Passcode: SYNTHETIC_MEETING_SECRET"))
    data["textBytes"] += len(data["slides"][0]["notes"][-1]["text"].encode())
    client, _ = client_for(data)
    document = client.read_document(META)
    source = c.normalize_drive_document(META, document)
    assert "SYNTHETIC_MEETING_SECRET" not in json.dumps(document)
    assert source["authorship"] == "unverified" and source["authored_text"] == ""
    assert source["metadata"]["native_revision"] == "revision-1"
    assert "r=0.42, p=0.03" in source["body"]


def test_word_art_text_inside_groups_and_notes_is_preserved_with_exact_schema():
    data = payload()
    for section in ("elements", "notes"):
        value = record("word:" + section, "WordArt " + section + " text.", kind="word_art", groupPath=["group:" + section])
        data["slides"][0][section].append(value)
        data["textBytes"] += len(value["text"].encode())
    client, _ = client_for(data)
    text = client.read_document(META)["extracted_text"]
    assert "[word art word:elements groups group:elements]\nWordArt elements text." in text
    assert "[word art word:notes groups group:notes]\nWordArt notes text." in text
    data["slides"][0]["elements"][-1]["placeholderType"] = "BODY"
    client, _ = client_for(data)
    with pytest.raises(c.ConnectorError, match="document_slide_response_invalid"):
        client.read_document(META)


def test_view_only_revision_unavailable_keeps_exact_limit_and_drive_verification():
    data = payload()
    data.update(revisionId="", revisionAndOrderRechecked=False)
    data["limitations"].append("revision_id_unavailable")
    client, calls = client_for(data)
    extraction = client.read_document(META)["extraction"]
    assert len(calls) == 2
    assert extraction["revision_id_available"] is False
    assert extraction["slides_revision_and_order_rechecked"] is False
    assert extraction["slides_order_rechecked"] is True
    assert extraction["drive_metadata_rechecked"] is True
    assert "revision_id_unavailable" in extraction["limitations"]


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(schemaVersion=True),
    lambda p: p.update(presentationId="another-deck"),
    lambda p: p.update(unexpectedPrivateField="not accepted"),
    lambda p: p.update(slideCount=1),
    lambda p: p.update(textBytes=p["textBytes"] + 1),
    lambda p: p.update(slideOrderRechecked=False),
    lambda p: p.update(revisionAndOrderRechecked=False),
    lambda p: p.update(revisionId=""),
    lambda p: p.update(revisionId="", revisionAndOrderRechecked=False),
    lambda p: p.update(partialText=False),
    lambda p: p["limitations"].append("not_a_known_limitation"),
    lambda p: p["slides"][1].update(objectId=p["slides"][0]["objectId"]),
    lambda p: p["slides"][0].update(number=2),
    lambda p: p["slides"][0].update(notes=None),
    lambda p: p["slides"][0]["elements"][0].update(groupPath=None),
    lambda p: p["slides"][0]["elements"][0].update(kind=["shape"]),
    lambda p: p["slides"][0]["elements"][1].update(rowIndex=True),
    lambda p: p["slides"][0]["elements"][1].update(cellIndex=-1),
    lambda p: p["slides"][0]["elements"].append(deepcopy(p["slides"][0]["elements"][1])),
    lambda p: p["slides"][0]["elements"][0].update(text="\ud800"),
])
def test_incomplete_or_inconsistent_schema_never_falls_back_or_discards_cache(tmp_path, mutate):
    data = payload()
    mutate(data)
    cache = incomplete_cache(tmp_path)
    before = cache.read_bytes()
    client, calls = client_for(data, cache_home=tmp_path)
    with pytest.raises(c.ConnectorError, match="document_slide_response_invalid"):
        client.read_document(META)
    assert len(calls) == 1
    assert cache.read_bytes() == before


@pytest.mark.parametrize("marker,code,discard", [
    ("auth_required", "gog_auth_required", False),
    ("auth_unavailable", "gog_auth_required", False),
    ("rate_limited", "gog_rate_limited", False),
    ("quota_exceeded", "gog_quota_exceeded", False),
    ("read_failed", "gog_read_failed", False),
    ("byte_budget_exceeded", "document_response_budget_exceeded", False),
    ("response_invalid", "document_slide_response_invalid", False),
    ("access_denied", "source_access_denied", True),
    ("not_found", "source_not_found", True),
    ("changed_during_read", "document_changed_during_read", True),
])
def test_fixed_failure_classes_do_not_fallback_and_preserve_or_invalidate_cache(tmp_path, marker, code, discard):
    cache = incomplete_cache(tmp_path)
    before = cache.read_bytes()
    client, calls = client_for(failure="slides_text_" + marker, cache_home=tmp_path)
    with pytest.raises(c.ConnectorError) as error:
        client.read_document(META)
    assert error.value.code == code
    assert error.value.operation == "slides.read-presentation-text"
    assert "SYNTHETIC_PRIVATE_STDERR" not in str(error.value)
    assert len(calls) == 1
    if discard:
        assert not cache.exists()
    else:
        assert cache.read_bytes() == before


@pytest.mark.parametrize("change", [{"id": "different"}, {"mimeType": "text/plain"},
                                     {"modifiedTime": "2026-09-13T00:01:00Z"}, {"trashed": True}])
def test_final_drive_change_or_deletion_invalidates_cache_and_returns_no_body(tmp_path, change):
    cache = incomplete_cache(tmp_path)
    client, calls = client_for(final_metadata={**META, **change}, cache_home=tmp_path)
    with pytest.raises(c.ConnectorError, match="source_access_denied|document_changed_during_read"):
        client.read_document(META)
    assert len(calls) == 2
    assert not cache.exists()


@pytest.mark.parametrize("raw", ['{"schemaVersion":1,"schemaVersion":1}', '{"value":NaN}', '[]', 'not json'])
def test_ambiguous_or_malformed_json_is_rejected_without_fallback(raw):
    client, calls = client_for(raw)
    with pytest.raises(c.ConnectorError, match="gog_invalid_json|document_slide_response_invalid"):
        client.read_document(META)
    assert len(calls) == 1


def test_output_budget_is_independently_enforced():
    client, calls = client_for(" " * (c._DOCUMENT_BYTES + 1))
    with pytest.raises(c.ConnectorError, match="document_response_budget_exceeded"):
        client.read_document(META)
    assert len(calls) == 1


def test_missing_drive_revision_fails_before_execution():
    client, calls = client_for()
    with pytest.raises(c.ConnectorError, match="document_slide_revision_required"):
        client.read_document({k: v for k, v in META.items() if k != "modifiedTime"})
    assert calls == []


def test_alternate_executable_cannot_be_reached_through_general_call_or_other_formats():
    runner = Mock(return_value=subprocess.CompletedProcess([], 0, '{"document":{"tabs":[]}}', ""))
    client = c.GogClient(ACCOUNT, executable=BASE, slides_text_executable=ALTERNATE, runner=runner)
    with pytest.raises(c.ConnectorError, match="gog_command_not_readonly"):
        client._call(("slides", "read-presentation-text"), ["deck"])
    assert client.read_document({"id": "doc", "mimeType": "application/vnd.google-apps.document"}) == {"tabs": []}
    assert runner.call_count == 1 and runner.call_args.args[0][0] == BASE


@pytest.mark.parametrize("value", ["relative/gog", "", "--unsafe", "/some/path\x00", 123])
def test_explicit_executable_requires_absolute_path(value):
    with pytest.raises(c.ConnectorError, match="gog_slides_text_executable_invalid"):
        c.GogClient(ACCOUNT, slides_text_executable=value)


def test_sync_passes_explicit_option_only_to_drive(tmp_path, monkeypatch):
    store = Store(tmp_path / "private")
    try:
        config = {"account": ACCOUNT, "drive_slides_text_executable": ALTERNATE}
        client = object()
        factory = Mock(return_value=client)
        monkeypatch.setattr("tools.cha_philosophy.sync.GogClient", factory)
        operation = Mock(return_value={"platform": "drive", "state": "partial"})
        monkeypatch.setattr("tools.cha_philosophy.sync.sync_drive", operation)
        result = sync(store, config, "drive", 1)
        factory.assert_called_once_with(ACCOUNT, document_cache_home=store.home, slides_text_executable=ALTERNATE)
        operation.assert_called_once_with(store, config, client, 1)
        assert result["results"][0]["state"] == "partial"
    finally:
        store.close()


@pytest.mark.parametrize("code", ["document_slide_revision_required", "document_slide_node_budget_exceeded",
                                  "document_slide_group_depth_exceeded", "document_slide_count_budget_exceeded",
                                  "gog_slides_text_executable_invalid"])
def test_new_fixed_failures_survive_source_free_status_summary(code):
    from tools.cha_philosophy.refresh import _source_summary
    summary = _source_summary("drive", {"results": [{"platform": "drive", "state": "error",
                              "error_code": code, "source_id": "SYNTHETIC_PRIVATE_ID",
                              "body": "SYNTHETIC_PRIVATE_SOURCE"}]})
    assert summary["error_code"] == code
    assert summary["has_errors"] and not summary["attempt_succeeded"]
    assert "SYNTHETIC_PRIVATE" not in json.dumps(summary)
