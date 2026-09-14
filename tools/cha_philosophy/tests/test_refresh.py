"""Refresh scheduling boundaries with synthetic stores and mocked providers."""
import fcntl
import json
import os
from unittest.mock import Mock

import pytest

from tools.cha_philosophy.distill import extractor_fingerprint
from tools.cha_philosophy.refresh import local_ollama_available, refresh
from tools.cha_philosophy.store import Store

CONFIG = {"account": "professor@example.invalid", "identities": ["professor@example.invalid"],
          "lab_recipients": ["student@example.invalid"], "teams_team_ids": ["synthetic-team"],
          "teams_author_names": ["Synthetic Professor"], "teams_professor_ids": ["synthetic-professor"],
          "teams_tenant_id": "synthetic-tenant"}


def source():
    return {"source_id": "fixture:a", "platform": "fixture", "scope": "synthetic", "author_id": "professor",
            "authorship": "direct", "status": "active", "body": "주장의 강도는 근거 수준에 맞춥니다.",
            "authored_text": "주장의 강도는 근거 수준에 맞춥니다."}


def successful_sync(store, config, platform, limit):
    states = {"gmail": "partial", "drive": "complete_for_query", "teams": "complete_configured_channels",
              "teams_archive": "historical_context_only"}
    return {"results": [{"platform": platform, "state": states[platform], "changed_sources": 1,
                          "sources": 1469 if platform == "teams_archive" else 1,
                          "processed_threads": limit if platform == "gmail" else 0}],
            "overall_complete": False}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    store = Store(tmp_path / "private")
    store.upsert(source())
    monkeypatch.delenv("CHA_TEAMS_ACCESS_TOKEN", raising=False)
    sync = Mock(side_effect=successful_sync)
    inference = Mock(return_value={"results": [], "pending": 1, "ready": 1, "held": 0, "deferred": 0})
    probe = Mock(return_value=False)
    monkeypatch.setattr("tools.cha_philosophy.refresh.sync", sync)
    monkeypatch.setattr("tools.cha_philosophy.refresh.distill_pending", inference)
    monkeypatch.setattr("tools.cha_philosophy.refresh.local_ollama_available", probe)
    monkeypatch.setattr("tools.cha_philosophy.refresh._now", lambda: "2026-09-13T00:00:00+00:00")
    try:
        yield store, sync, inference, probe
    finally:
        store.close()


def test_default_budgets_and_unavailable_model_never_consume_retry(setup):
    store, sync, inference, probe = setup
    src = store.get_source("fixture:a")
    store.mark_extraction(src, "failed", attempts=2, extractor_id=extractor_fingerprint(),
                          next_retry_at="2020-01-01T00:00:00+00:00")
    previous = store.extraction_record(src)
    result = refresh(store, CONFIG)
    assert sorted((c.kwargs["platform"], c.kwargs["limit"]) for c in sync.call_args_list) == [("drive", 10), ("gmail", 100)]
    inference.assert_not_called()
    probe.assert_called_once()
    assert result["inference"]["state"] == "backend_unavailable"
    assert result["inference"]["retry_state_unchanged"] is True
    assert store.extraction_record(src) == previous
    assert result["sources"]["teams"]["state"] == "not_configured"
    coverage = json.loads(store.db.execute("SELECT data FROM coverage WHERE scope='teams:refresh_attempt'").fetchone()[0])
    assert coverage["error_code"] == "teams_token_missing_or_invalid"
    assert result["overall_complete"] is False
    assert result["automatic_activation"] is False
    assert result["external_notifications"] is False


def test_completed_chat_and_channel_routes_are_accounted_without_private_details(setup,monkeypatch):
    store,sync,_,_=setup
    monkeypatch.setenv("CHA_TEAMS_ACCESS_TOKEN","synthetic")
    def provider(store,config,platform,limit):
        if platform!="teams":return successful_sync(store,config,platform,limit)
        return {"results":[{"platform":"teams","state":"complete_configured_channels_and_chats",
            "channels":2,"chats":3,"channel_sync":{"state":"complete_configured_channels"},
            "chat_sync":{"state":"complete_configured_chats","chats":3,"errors":[],"chat_id":"private-chat-id"}}]}
    sync.side_effect=provider
    result=refresh(store,{**CONFIG,"teams_include_chats":True})
    assert result["sources"]["teams"]["chats"]==3
    assert result["sources"]["teams"]["chat_sync"]["state"]=="complete_configured_chats"
    assert "teams_private_chat_inventory_not_exhaustive" not in result["scope_gaps"]
    assert "private-chat-id" not in json.dumps(result)
    assert result["overall_complete"] is False


def test_status_file_private_atomic_and_source_free(setup):
    store, sync, _, _ = setup
    def provider(*args, **kwargs):
        platform = kwargs["platform"]
        return {"results": [{"platform": platform, "state": "error", "error_code": "private_secret_token",
                              "error_type": "Private Content", "body": "private raw mail",
                              "source_id": "secret-document-id", "limitations": ["private other text"]}]}
    sync.side_effect = provider
    result = refresh(store, CONFIG)
    path = store.home / "refresh_status.json"
    assert path.stat().st_mode & 0o777 == 0o600
    text = path.read_text()
    assert json.loads(text) == result
    assert "private_secret" not in text and "private raw" not in text and "secret-document" not in text
    assert result["sources"]["gmail"]["error_code"] == "source_sync_failed"
    assert not list(store.home.glob(".refresh-status-*.tmp"))
    assert (store.home / "refresh.lock").stat().st_mode & 0o777 == 0o600


def test_config_file_is_loaded_without_echoing_secrets(setup):
    store, sync, _, _ = setup
    config = {**CONFIG, "unused_secret": "private-config-secret"}
    (store.home / "config.json").write_text(json.dumps(config))
    result = refresh(store)
    assert sync.call_args_list[0].args[1] == config
    assert "private-config-secret" not in json.dumps(result)


def test_config_load_error_is_explicit_and_does_not_hide_inference_status(setup):
    store, sync, inference, _ = setup
    (store.home / "config.json").write_text("broken private configuration")
    result = refresh(store)
    sync.assert_not_called()
    inference.assert_not_called()
    assert result["sources"]["config"]["error_code"] == "config_load_failed"
    assert result["inference"]["state"] == "backend_unavailable"
    assert "broken private" not in json.dumps(result)


def test_graph_runs_only_with_explicit_environment_and_uses_bounded_channels(setup, monkeypatch):
    store, sync, _, _ = setup
    monkeypatch.setenv("CHA_TEAMS_ACCESS_TOKEN", "synthetic-token-never-log")
    result = refresh(store, CONFIG, gmail_limit=4, drive_limit=2)
    calls = [(c.kwargs["platform"], c.kwargs["limit"]) for c in sync.call_args_list]
    assert sorted(calls) == [("drive", 2), ("gmail", 4), ("teams", 3)]
    assert result["sources"]["teams"]["state"] == "complete_configured_channels"
    assert "synthetic-token-never-log" not in json.dumps(result)


def test_archive_skips_unchanged_mtime_and_imports_changed_file_once(setup, tmp_path):
    store, sync, _, _ = setup
    archive = tmp_path / "archive.sqlite3"
    archive.write_bytes(b"synthetic archive fixture")
    config = {**CONFIG, "teams_archive": str(archive)}
    first = refresh(store, config)
    assert first["sources"]["teams_archive"]["state"] == "historical_context_only"
    assert sum(c.kwargs["platform"] == "teams_archive" for c in sync.call_args_list) == 1
    second = refresh(store, config)
    assert second["sources"]["teams_archive"]["state"] == "unchanged"
    assert second["sources"]["teams_archive"]["processed_sources"] == 0
    assert sum(c.kwargs["platform"] == "teams_archive" for c in sync.call_args_list) == 1
    stat = archive.stat()
    os.utime(archive, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    third = refresh(store, config)
    assert third["sources"]["teams_archive"]["state"] == "historical_context_only"
    assert sum(c.kwargs["platform"] == "teams_archive" for c in sync.call_args_list) == 2
    assert second["last_success_times"]["teams_archive"] == first["last_success_times"]["teams_archive"]


def test_archive_config_scope_change_and_sqlite_wal_change_trigger_reimport(setup, tmp_path):
    store, sync, _, _ = setup
    archive = tmp_path / "archive.sqlite3"
    archive.write_bytes(b"fixture")
    config = {**CONFIG, "teams_archive": str(archive)}
    refresh(store, config)
    expanded = {**config, "teams_team_ids": ["synthetic-team", "synthetic-other-team"]}
    refresh(store, expanded)
    archive.with_name(archive.name + "-wal").write_bytes(b"new committed sqlite WAL data")
    refresh(store, expanded)
    assert sum(c.kwargs["platform"] == "teams_archive" for c in sync.call_args_list) == 3


def test_failed_archive_import_does_not_advance_checkpoint(setup, tmp_path):
    store, sync, _, _ = setup
    archive = tmp_path / "archive.sqlite3"
    archive.write_bytes(b"fixture")
    def provider(*args, **kwargs):
        if kwargs["platform"] == "teams_archive":
            return {"results": [{"platform": "teams_archive", "state": "error", "error_code": "sync_failed"}]}
        return successful_sync(*args, **kwargs)
    sync.side_effect = provider
    config = {**CONFIG, "teams_archive": str(archive)}
    result = refresh(store, config)
    assert result["sources"]["teams_archive"]["state"] == "error"
    assert store.checkpoint("refresh:teams_archive") is None
    refresh(store, config)
    assert sum(c.kwargs["platform"] == "teams_archive" for c in sync.call_args_list) == 2


def test_source_failure_does_not_stop_other_platforms_or_overwrite_last_success(setup, monkeypatch):
    store, sync, _, _ = setup
    first = refresh(store, CONFIG)
    def provider(*args, **kwargs):
        if kwargs["platform"] == "gmail":
            raise RuntimeError("private mailbox body from failed provider")
        return successful_sync(*args, **kwargs)
    sync.side_effect = provider
    monkeypatch.setattr("tools.cha_philosophy.refresh._now", lambda: "2026-09-13T01:00:00+00:00")
    second = refresh(store, CONFIG)
    assert second["sources"]["gmail"]["state"] == "error"
    assert second["health"] == "source_error"
    assert second["sources"]["drive"]["state"] == "complete_for_query"
    assert second["last_success_times"]["gmail"] == first["last_success_times"]["gmail"]
    assert second["last_success_times"]["drive"] == "2026-09-13T01:00:00+00:00"
    assert "private mailbox" not in json.dumps(second)


def test_partial_child_failure_is_visible_and_not_recorded_as_success(setup,monkeypatch):
    store,sync,_,_=setup
    first=refresh(store,CONFIG)
    def provider(*args,**kwargs):
        if kwargs['platform']=='gmail':
            return {'results':[{'platform':'gmail','state':'partial','processed_threads':5,
                'supported_evidence_refresh':{'state':'partial','failures':[{'error_code':'gog_auth_required'}]}}]}
        return successful_sync(*args,**kwargs)
    sync.side_effect=provider
    second=refresh(store,CONFIG)
    assert second['sources']['gmail']['has_errors'] is True
    assert second['sources']['gmail']['attempt_succeeded'] is False
    assert second['sources']['gmail']['supported_evidence_refresh']['failure_codes']=={'gog_auth_required':1}
    assert second['health']=='source_error'
    assert second['last_success_times']['gmail']==first['last_success_times']['gmail']


def test_inference_runs_small_batch_without_promoting_or_logging_sources(setup, monkeypatch):
    store, _, inference, probe = setup
    probe.return_value = True
    src = store.get_source("fixture:a")
    pid = store.add_principle({"statement": "근거에 맞추어 판단한다.", "domains": ["review"],
                              "evidence": [{"source_id": src["source_id"], "source_hash": src["source_hash"],
                                            "quote": src["authored_text"]}]})
    monkeypatch.setattr(store, "review", Mock(side_effect=AssertionError("must never review")))
    inference.return_value = {
        "results": [{"source_id": "private-source-id", "state": "complete", "candidates": 1, "inference_calls": 2},
                    {"source_id": "private-other-id", "state": "failed", "error_code": "quote_mismatch", "inference_calls": 2}],
        "pending": 8, "ready": 6, "deferred": 1, "held": 1}
    result = refresh(store, CONFIG)
    assert inference.call_args.kwargs["limit"] == 3
    assert result["inference"]["state"] == "partial"
    assert result["inference"]["completed_sources"] == 1
    assert result["inference"]["inference_calls"] == 4
    assert result["inference"]["failure_codes"] == {"quote_mismatch": 1}
    assert result["candidates_awaiting_review"] == 1
    assert store.get_principle(pid)["status"] == "candidate"
    assert "private-source" not in json.dumps(result)
    store.review.assert_not_called()


def test_drive_gap_details_keep_safe_reason_without_file_identity(setup):
    store, sync, _, _ = setup
    def provider(*args, **kwargs):
        result = successful_sync(*args, **kwargs)
        if kwargs["platform"] == "drive":
            result["results"][0].update(state="complete_for_query_with_content_gaps", document_read_gaps=[
                {"file_id": "private-document-id", "reason": "document_pdf_requires_ocr"}])
        return result
    sync.side_effect = provider
    result = refresh(store, CONFIG)
    assert result["sources"]["drive"]["content_gap_count"] == 1
    assert result["sources"]["drive"]["content_gap_codes"] == {"document_pdf_requires_ocr": 1}
    assert "private-document-id" not in json.dumps(result)
    assert "drive_content_gaps" in result["scope_gaps"]


@pytest.mark.parametrize("code", ["gog_auth_required", "gog_connect_timeout"])
def test_source_failure_code_survives_refresh_and_saved_status(setup, code):
    store, sync, _, _ = setup
    def provider(*args, **kwargs):
        if kwargs["platform"] == "gmail":
            return {"results": [{"platform": "gmail", "state": "error", "error_code": code}]}
        return successful_sync(*args, **kwargs)
    sync.side_effect = provider
    result = refresh(store, CONFIG)
    assert result["sources"]["gmail"]["error_code"] == code
    saved = json.loads((store.home / "refresh_status.json").read_text())
    assert saved["sources"]["gmail"]["error_code"] == code


def test_disabled_extraction_does_not_probe_or_call_model(setup):
    store, _, inference, probe = setup
    result = refresh(store, CONFIG, distill_limit=0)
    assert result["inference"]["state"] == "disabled"
    probe.assert_not_called()
    inference.assert_not_called()


def test_outer_lock_prevents_overlap_and_does_not_overwrite_existing_status(setup):
    store, sync, inference, _ = setup
    status = store.home / "refresh_status.json"
    status.write_text('{"previous":"fixture"}')
    with (store.home / "refresh.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = refresh(store, CONFIG)
    assert result["state"] == "already_running"
    assert status.read_text() == '{"previous":"fixture"}'
    sync.assert_not_called()
    inference.assert_not_called()


def test_refresh_outer_lock_is_separate_from_sync_lock(setup):
    store, sync, _, _ = setup
    def provider(*args, **kwargs):
        with (store.home / "sync.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return successful_sync(*args, **kwargs)
    sync.side_effect = provider
    result = refresh(store, CONFIG)
    assert result["sources"]["gmail"]["state"] == "partial"


def test_interrupted_status_replace_preserves_previous_json(setup, monkeypatch):
    store, _, _, _ = setup
    status = store.home / "refresh_status.json"
    status.write_text('{"old":"complete-json"}')
    monkeypatch.setattr("tools.cha_philosophy.refresh.os.replace", Mock(side_effect=OSError("write failed")))
    with pytest.raises(OSError):
        refresh(store, CONFIG)
    assert status.read_text() == '{"old":"complete-json"}'
    assert not list(store.home.glob(".refresh-status-*.tmp"))


def test_local_probe_no_proxy_no_redirect_and_only_metadata(monkeypatch):
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = b'{"version":"synthetic-version"}'
    opener = Mock()
    opener.open.return_value = response
    builder = Mock(return_value=opener)
    monkeypatch.setattr("tools.cha_philosophy.refresh.urllib.request.build_opener", builder)
    assert local_ollama_available() is True
    assert opener.open.call_args.args == ("http://127.0.0.1:11434/api/version",)
    assert opener.open.call_args.kwargs["timeout"] == 3
    assert builder.call_args.args[0].proxies == {}
    assert builder.call_args.args[1].redirect_request(None, None, None, None, None, None) is None
    opener.open.side_effect = TimeoutError("private transport context")
    assert local_ollama_available() is False


@pytest.mark.parametrize("kwargs", [{"gmail_limit": 0}, {"drive_limit": True}, {"distill_limit": -1}])
def test_invalid_budget_rejected_before_any_provider_call(setup, kwargs):
    store, sync, inference, _ = setup
    with pytest.raises(ValueError):
        refresh(store, CONFIG, **kwargs)
    sync.assert_not_called()
    inference.assert_not_called()
