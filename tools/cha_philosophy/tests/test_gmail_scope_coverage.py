"""Changing a Gmail roster retires reports while preserving source history."""
from copy import deepcopy
import json

from tools.cha_philosophy.store import digest
from tools.cha_philosophy.sync import _gmail_query, sync_gmail
from tools.cha_philosophy.tests.test_sync import (
    CONFIG, GmailFixture, add_supported_gmail, expire_refresh_time, opened,
)


def reports(store):
    return {row["scope"]: json.loads(row["data"]) for row in store.db.execute("SELECT scope,data FROM coverage")}


def expanded_config():
    return {**CONFIG, "lab_recipients": [*CONFIG["lab_recipients"], "historical-member@example.invalid"]}


def test_changed_gmail_query_supersedes_report_without_deleting_old_sources_or_checkpoint(tmp_path):
    with opened(tmp_path / "store") as store:
        old_result = sync_gmail(store, CONFIG, GmailFixture({"": {"threads": [{"id": "old"}, {"id": "later"}]}}), 1)
        sid, pid = add_supported_gmail(store, "old")
        old_source = deepcopy(store.get_source(sid))
        old_checkpoint = deepcopy(store.checkpoint(old_result["sync_scope"]))
        result = sync_gmail(store, expanded_config(), GmailFixture({"": {"threads": []}}), 10)
        old_report = reports(store)[old_result["sync_scope"]]
        assert old_report["state"] == old_report["scope_status"] == "superseded"
        assert old_report["previous_state"] == "partial"
        assert old_report["superseded_by"] == result["sync_scope"]
        assert old_report["inventory_query"] == old_result["inventory_query"]
        assert old_report["pending_threads"] == 1
        assert store.checkpoint(old_result["sync_scope"]) == old_checkpoint
        assert store.get_source(sid) == old_source
        assert store.get_principle(pid)["status"] == "evidence_supported"
        assert result["scope_status"] == "active" and result["state"] == "complete_for_query"
        assert result["inventory_query"] == result["cycle_query"] == _gmail_query(expanded_config())


def test_other_accounts_and_auxiliary_reports_are_unchanged(tmp_path):
    with opened(tmp_path / "store") as store:
        old_scope = "gmail:" + CONFIG["account"] + ":" + digest(_gmail_query(CONFIG))[:12]
        foreign_scope = "gmail:other@example.invalid:0123456789ab"
        auxiliary_scope = "gmail:" + CONFIG["account"] + ":delivery_health"
        foreign = {"platform": "gmail", "state": "complete_for_query", "marker": "foreign"}
        auxiliary = {"state": "ready", "marker": "auxiliary"}
        store.coverage(old_scope, {"platform": "gmail", "state": "partial"})
        store.coverage(foreign_scope, foreign)
        store.coverage(auxiliary_scope, auxiliary)
        store.checkpoint(foreign_scope, {"pending": ["foreign-thread"], "in_progress": True})
        foreign_checkpoint = store.checkpoint(foreign_scope)
        result = sync_gmail(store, expanded_config(), GmailFixture({"": {"threads": []}}), 1)
        current = reports(store)
        assert current[foreign_scope] == foreign
        assert current[auxiliary_scope] == auxiliary
        assert store.checkpoint(foreign_scope) == foreign_checkpoint
        assert current[old_scope]["superseded_by"] == result["sync_scope"]
        assert current["gmail:supported_evidence_refresh"]["state"] == "complete_due_supported_evidence"


def test_supported_evidence_refresh_still_precedes_new_scope_backfill(tmp_path):
    with opened(tmp_path / "store") as store:
        old = sync_gmail(store, CONFIG, GmailFixture({"": {"threads": [{"id": "curated"}]}}), 10)
        sid, pid = add_supported_gmail(store, "curated")
        previous_verified_at = expire_refresh_time(store, sid)
        client = GmailFixture({"": {"threads": [{"id": "backlog-a"}, {"id": "curated"}]}})
        result = sync_gmail(store, expanded_config(), client, 1)
        assert client.thread_calls == ["curated", "backlog-a"]
        assert result["supported_evidence_refresh"]["refreshed_threads"] == 1
        assert store.get_source(sid)["verified_at"] != previous_verified_at
        assert store.get_source(sid)["status"] == "active"
        assert store.get_principle(pid)["status"] == "evidence_supported"
        assert reports(store)[old["sync_scope"]]["state"] == "superseded"
        final = sync_gmail(store, expanded_config(), client, 1)
        assert final["state"] == "complete_for_query"
        assert store.get_principle(pid)["status"] == "evidence_supported"


def test_repeated_scope_does_not_rewrite_superseded_timestamp_and_switchback_resumes(tmp_path):
    with opened(tmp_path / "store") as store:
        original_client = GmailFixture({"": {"threads": [{"id": "a"}, {"id": "b"}]}})
        original = sync_gmail(store, CONFIG, original_client, 1)
        newer_client = GmailFixture({"": {"threads": [{"id": "c"}, {"id": "d"}]}})
        newer = sync_gmail(store, expanded_config(), newer_client, 1)
        superseded_report = deepcopy(reports(store)[original["sync_scope"]])
        sync_gmail(store, expanded_config(), newer_client, 1)
        assert reports(store)[original["sync_scope"]] == superseded_report
        resumed = sync_gmail(store, CONFIG, original_client, 1)
        assert resumed["sync_scope"] == original["sync_scope"]
        assert resumed["cycle_threads"] == 2
        assert original_client.page_calls == [""] and original_client.thread_calls == ["a", "b"]
        assert "superseded_by" not in reports(store)[original["sync_scope"]]
        assert reports(store)[newer["sync_scope"]]["superseded_by"] == original["sync_scope"]


def test_existing_order_sensitive_query_and_checkpoint_hash_remain_compatible(tmp_path):
    config = {**CONFIG, "identities": ["z@example.invalid", CONFIG["account"]],
              "lab_recipients": ["z-student@example.invalid", *CONFIG["lab_recipients"]]}
    query = "in:sent {from:z@example.invalid from:" + CONFIG["account"] + "} {to:z-student@example.invalid cc:z-student@example.invalid bcc:z-student@example.invalid to:lab-member@example.invalid cc:lab-member@example.invalid bcc:lab-member@example.invalid}"
    scope = "gmail:" + CONFIG["account"] + ":" + digest(query)[:12]
    with opened(tmp_path / "store") as store:
        client = GmailFixture({"": {"threads": [{"id": "a"}, {"id": "b"}]}})
        first = sync_gmail(store, config, client, 1)
        assert first["sync_scope"] == scope and first["inventory_query"] == query
        assert store.checkpoint(scope)["pending"] == ["b"]
        resumed = sync_gmail(store, config, client, 1)
        assert resumed["sync_scope"] == scope and resumed["cycle_threads"] == 2
        assert client.page_calls == [""]
