"""Restart/reconciliation behaviours with synthetic provider responses only."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import io
from unittest.mock import patch

import pytest

from tools.cha_philosophy.connectors import ConnectorError,TeamsGraphClient,SyncProgress,normalize_teams_native
from tools.cha_philosophy.store import Store
from tools.cha_philosophy.sync import refresh_supported_evidence, sync, sync_drive, sync_gmail, sync_teams


CONFIG = {
    "account": "professor@example.invalid",
    "identities": ["professor@example.invalid"],
    "lab_recipients": ["lab-member@example.invalid"],
    "drive_account_is_professor": True,
    "teams_team_ids": ["synthetic-team"],
    "teams_tenant_id": "synthetic-tenant",
    "teams_professor_ids": ["synthetic-professor-id"],
}


@contextmanager
def opened(home):
    store = Store(home)
    try:
        yield store
    finally:
        store.close()


def thread(tid):
    return {"_account_id": CONFIG["account"], "thread": {
        "id": tid,
        "messages": [{"id": "message-" + tid, "labelIds": ["SENT"],
                      "from": CONFIG["account"], "to": CONFIG["lab_recipients"][0],
                      "subject": "Synthetic research guidance",
                      "internalDate": "1700000000000",
                      "body": "Use evidence when interpreting research. Thread " + tid}],
    }}


class GmailFixture:
    def __init__(self, pages, fail_once=None):
        self.pages = pages
        self.fail_once = set(fail_once or [])
        self.page_calls = []
        self.thread_calls = []

    def gmail_page(self, query, page_token=""):
        self.page_calls.append(page_token)
        return deepcopy(self.pages[page_token])

    def get_gmail_thread(self, tid):
        self.thread_calls.append(tid)
        if tid in self.fail_once:
            self.fail_once.remove(tid)
            raise ConnectorError("synthetic_temporary_read_failure")
        return thread(tid)


def test_gmail_pending_page_survives_process_restarts_and_item_limit(tmp_path):
    client = GmailFixture({
        "": {"threads": [{"id": x} for x in ("a", "b", "c")], "nextPageToken": "page-2"},
        "page-2": {"threads": [{"id": x} for x in ("d", "e")]},
    })
    home = tmp_path / "store"
    states = []
    counts = []
    for _ in range(3):
        with opened(home) as store:
            result = sync_gmail(store, CONFIG, client, limit=2)
            states.append(result["state"])
            counts.append(len(list(store.sources(direct=True))))
    assert states == ["partial", "partial", "complete_for_query"]
    assert counts == [2, 4, 5]
    assert client.thread_calls == ["a", "b", "c", "d", "e"]
    assert client.page_calls == ["", "page-2"]


def test_failed_gmail_thread_is_retried_without_losing_pending_work(tmp_path):
    client = GmailFixture({"": {"threads": [{"id": x} for x in ("a", "b", "c")]}},
                          fail_once={"b"})
    home = tmp_path / "store"
    with opened(home) as store:
        with pytest.raises(ConnectorError, match="temporary_read_failure"):
            sync_gmail(store, CONFIG, client, limit=10)
        assert [s["native_id"] for s in store.sources()] == ["message-a"]
    with opened(home) as store:
        result = sync_gmail(store, CONFIG, client, limit=10)
        assert result["state"] == "complete_for_query"
        assert {s["native_id"] for s in store.sources()} == {"message-a", "message-b", "message-c"}
    assert client.thread_calls == ["a", "b", "b", "c"]
    assert client.page_calls == [""]


class DriveFixture:
    def __init__(self, pages):
        self.pages = pages
        self.page_calls = []

    def drive_page(self, query, page_token=""):
        self.page_calls.append(page_token)
        return deepcopy(self.pages[page_token])

    def get_drive_metadata(self, fid):
        available = {item["id"] for page in self.pages.values() for item in page["files"]}
        if fid not in available:
            raise ConnectorError("drive_source_unavailable")
        return {"id": fid, "name": "Synthetic research draft " + fid,
                "mimeType": "application/vnd.google-apps.document", "trashed": False}

    def get_drive_document(self, fid):
        return {"documentId": fid, "tabs": [{
            "tabProperties": {"tabId": "tab-1", "title": "Draft"},
            "documentTab": {"body": {"content": [{"paragraph": {"elements": [
                {"textRun": {"content": "Synthetic shared draft body."}}
            ]}}]}},
        }]}

    def read_document(self, filemeta):
        return self.get_drive_document(filemeta["id"])

    def iter_drive_comments(self, fid, *, progress=None):
        yield {"id": "comment-" + fid, "author": {"me": True},
               "content": "Claims must follow the evidence in this manuscript.",
               "createdTime": "2025-01-01T00:00:00Z", "replies": []}
        if progress is not None:
            progress.complete = True


def activate_comment(store, fid):
    src = next(s for s in store.sources(direct=True, platform="drive")
               if s["metadata"]["file_id"] == fid)
    pid = store.add_principle({
        "statement": "Claims must follow the evidence.", "domains": ["review"],
        "evidence": [{"source_id": src["source_id"], "quote": src["authored_text"],
                      "source_hash": src["source_hash"]}],
    })
    store.review(pid, "evidence_supported", "synthetic reviewer", "Exact scoped evidence checked.")
    assert len(store.bundle("review", "evidence")["principles"]) == 1
    return src["source_id"], pid


def test_drive_file_missing_from_complete_inventory_withdraws_old_evidence(tmp_path):
    home = tmp_path / "store"
    with opened(home) as store:
        first = sync_drive(store, CONFIG, DriveFixture({"": {"files": [{"id": "old-file"}]}}), limit=10)
        assert first["state"] == "complete_for_query"
        sid, pid = activate_comment(store, "old-file")
    with opened(home) as store:
        result = sync_drive(store, CONFIG, DriveFixture({"": {"files": []}}), limit=10)
        assert result["state"] == "complete_for_query"
        assert store.get_source(sid)["status"] == "unavailable"
        assert store.get_source(sid)["body"] == ""
        assert store.get_principle(pid)["evidence"] == []
        assert not store.bundle("review", "evidence")["principles"]


def test_partial_drive_inventory_cannot_withdraw_a_file_on_a_later_page(tmp_path):
    home = tmp_path / "store"
    with opened(home) as store:
        sync_drive(store, CONFIG, DriveFixture({"": {"files": [{"id": "old-file"}]}}), limit=10)
        sid, pid = activate_comment(store, "old-file")
    client = DriveFixture({
        "": {"files": [{"id": "new-file"}], "nextPageToken": "later"},
        "later": {"files": [{"id": "old-file"}]},
    })
    with opened(home) as store:
        first = sync_drive(store, CONFIG, client, limit=1)
        assert first["state"] == "partial"
        assert store.get_source(sid)["status"] == "active"
        assert store.get_principle(pid)["status"] == "evidence_supported"
    with opened(home) as store:
        final = sync_drive(store, CONFIG, client, limit=1)
        assert final["state"] == "complete_for_query"
        assert store.get_source(sid)["status"] == "active"
        assert store.get_principle(pid)["status"] == "evidence_supported"
    assert client.page_calls == ["", "later"]


def test_provider_incomplete_drive_search_is_not_a_complete_deletion_inventory(tmp_path):
    home = tmp_path / "store"
    with opened(home) as store:
        sync_drive(store, CONFIG, DriveFixture({"": {"files": [{"id": "old-file"}]}}), limit=10)
        sid, pid = activate_comment(store, "old-file")
    client = DriveFixture({"": {"files": [], "incompleteSearch": True}})
    with opened(home) as store:
        try:
            result = sync_drive(store, CONFIG, client, limit=10)
        except ConnectorError:
            result = None  # Explicit failure with preserved checkpoint is acceptable.
        assert result is None or result["state"] != "complete_for_query"
        assert store.get_source(sid)["status"] == "active"
        assert store.get_principle(pid)["status"] == "evidence_supported"


def test_drive_default_and_null_query_cover_supported_formats(tmp_path):
    class Client(DriveFixture):
        def drive_page(self, query, page_token=""):
            self.query=query
            return super().drive_page(query,page_token)
    client=Client({"":{"files":[]}})
    with opened(tmp_path / "store") as store:
        result=sync_drive(store,{**CONFIG,"drive_query":None},client,10)
    assert result["inventory_complete"] is True
    for mime in ("application/pdf","application/vnd.google-apps.presentation","application/vnd.google-apps.spreadsheet",
                 "application/vnd.openxmlformats-officedocument.wordprocessingml.document"):
        assert mime in client.query


def test_drive_format_failure_preserves_document_and_processes_following_file(tmp_path):
    class Client(DriveFixture):
        def read_document(self, meta):
            if meta["id"]=="old-file": raise ConnectorError("document_pdf_requires_ocr")
            return {"extracted_text":"Slide speaker note context", "extraction":{"format":"pptx","limitations":["images_not_read"]}}
    with opened(tmp_path / "store") as store:
        sync_drive(store,CONFIG,DriveFixture({"":{"files":[{"id":"old-file"}]}}),10)
        old=next(s for s in store.sources(platform="drive") if s["metadata"]["source_kind"]=="document")
        client=Client({"":{"files":[{"id":"old-file"},{"id":"new-file"}]}})
        result=sync_drive(store,CONFIG,client,10)
        current=store.get_source(old["source_id"])
        assert current["status"]=="active" and current["verified_at"]==old["verified_at"]
        assert result["state"]=="complete_for_query_with_content_gaps"
        assert result["processed_files"]==2
        assert result["document_read_gaps"][0]["reason"]=="document_pdf_requires_ocr"
        assert "images_not_read" in result["limitations"]
        new=next(s for s in store.sources(platform="drive") if s["native_id"]=="new-file")
        assert new["body"]=="Slide speaker note context" and new["authorship"]=="unverified"


def test_explicit_unsupported_file_and_comments_error_do_not_poison_queue_or_revoke(tmp_path):
    class Client(DriveFixture):
        def get_drive_metadata(self,fid):
            return {**super().get_drive_metadata(fid),"mimeType":"application/x-hwp" if fid=="old-file" else "application/vnd.google-apps.document"}
        def iter_drive_comments(self,fid,*,progress=None):
            if fid=="old-file":raise ConnectorError("gog_read_failed")
            yield from super().iter_drive_comments(fid,progress=progress)
    with opened(tmp_path / "store") as store:
        sync_drive(store,CONFIG,DriveFixture({"":{"files":[{"id":"old-file"}]}}),10)
        sid,pid=activate_comment(store,"old-file")
        result=sync_drive(store,{**CONFIG,"drive_file_ids":["old-file"]},Client({"":{"files":[{"id":"old-file"},{"id":"new-file"}]}}),10)
        assert result["processed_files"]==2 and result["unsupported_document_formats"]==1
        assert result["document_read_gaps"][0]["comment_read_error"]=="gog_read_failed"
        assert store.get_source(sid)["status"]=="active"
        assert store.get_principle(pid)["status"]=="evidence_supported"


def test_drive_partial_slide_recovery_advances_backfill_and_reports_omitted_text(tmp_path):
    class Client(DriveFixture):
        def read_document(self, meta):
            return {"extracted_text":"Recovered slide and speaker notes", "extraction":{
                "format":"google_slides_native_text", "partial_text":True,
                "fallback_reason":"gog_native_export_timeout", "limitations":["group_and_table_text_not_extracted"]}}
    with opened(tmp_path / "store") as store:
        client=Client({"":{"files":[{"id":"large-deck"},{"id":"next-file"}]}})
        result=sync_drive(store,CONFIG,client,1)
        assert result["pending_files"]==1 and result["processed_files"]==1
        gap=result["document_read_gaps"][0]
        assert gap["file_id"]=="large-deck" and gap["reason"]=="document_partial_text_extraction"
        assert gap["fallback_reason"]=="gog_native_export_timeout"
        result=sync_drive(store,CONFIG,client,1)
        assert result["state"]=="complete_for_query_with_content_gaps"
        source=next(s for s in store.sources(platform="drive") if s["native_id"]=="large-deck")
        assert source["body"]=="Recovered slide and speaker notes" and source["authorship"]=="unverified"


@pytest.mark.parametrize("error_code", ["gog_read_failed", "gog_auth_required", "gog_rate_limited", "gog_quota_exceeded"])
def test_drive_retryable_failure_during_body_read_preserves_source_and_pending(tmp_path, error_code):
    class Client(DriveFixture):
        def read_document(self,meta):raise ConnectorError(error_code)
    with opened(tmp_path / "store") as store:
        sync_drive(store,CONFIG,DriveFixture({"":{"files":[{"id":"old-file"}]}}),10)
        old=next(s for s in store.sources(platform="drive") if s["metadata"]["source_kind"]=="document")
        client=Client({"":{"files":[{"id":"old-file"},{"id":"next-file"}]}})
        with pytest.raises(ConnectorError,match=error_code):
            sync_drive(store,CONFIG,client,10)
        current=store.get_source(old["source_id"])
        assert current["status"]=="active" and current["verified_at"]==old["verified_at"]
        pending=store.checkpoint(old["metadata"]["sync_scope"])["pending"]
        assert pending==["old-file","next-file"]


@pytest.mark.parametrize("error_code", ["source_access_denied", "source_not_found", "source_download_restricted", "gog_export_unsupported"])
def test_drive_document_access_gap_preserves_readable_comments_advances_and_retries(tmp_path, error_code):
    class Client(DriveFixture):
        def read_document(self, meta):
            if meta["id"] == "old-file":
                failure = ConnectorError(error_code)
                failure.operation = "drive.download"
                raise failure
            return super().read_document(meta)

    pages = {"": {"files": [{"id": "old-file"}, {"id": "next-file"}]}}
    with opened(tmp_path / "store") as store:
        sync_drive(store, CONFIG, DriveFixture({"": {"files": [{"id": "old-file"}]}}), 10)
        sid, pid = activate_comment(store, "old-file")
        body = next(s for s in store.sources(platform="drive") if s["metadata"]["source_kind"] == "document")
        result = sync_drive(store, CONFIG, Client(pages), 10)
        assert result["processed_files"] == 2 and result["pending_files"] == 0
        assert result["access_withdrawn"] == 0
        gap = result["document_read_gaps"][0]
        assert gap["reason"] == error_code and gap["stage"] == "document"
        assert gap["operation"] == "drive.download"
        assert gap["retry_policy"] == "next_full_inventory_cycle"
        current = store.get_source(body["source_id"])
        assert current["status"] == "active" and current["verified_at"] == body["verified_at"]
        assert store.get_source(sid)["status"] == "active"
        assert store.get_principle(pid)["status"] == "evidence_supported"
        retry = sync_drive(store, CONFIG, DriveFixture(pages), 10)
        assert retry["processed_files"] == 2 and retry["document_read_gaps"] == []


def test_drive_comment_access_gap_is_scoped_and_does_not_block_next_file(tmp_path):
    class Client(DriveFixture):
        def iter_drive_comments(self, fid, *, progress=None):
            if fid == "old-file":
                raise ConnectorError("source_access_denied")
            yield from super().iter_drive_comments(fid, progress=progress)

    with opened(tmp_path / "store") as store:
        sync_drive(store, CONFIG, DriveFixture({"": {"files": [{"id": "old-file"}]}}), 10)
        sid, _ = activate_comment(store, "old-file")
        prior = store.get_source(sid)
        result = sync_drive(store, CONFIG, Client({"": {"files": [{"id": "old-file"}, {"id": "next-file"}]}}), 10)
        assert result["processed_files"] == 2
        assert result["document_read_gaps"][0]["stage"] == "comments"
        assert result["document_read_gaps"][0]["comment_read_error"] == "source_access_denied"
        assert store.get_source(sid)["status"] == "unavailable"
        assert store.get_source(sid)["verified_at"] == prior["verified_at"]


def test_drive_access_denial_still_withdraws_supported_evidence(tmp_path):
    class Client(DriveFixture):
        def get_drive_metadata(self,fid):raise ConnectorError("source_access_denied")
    with opened(tmp_path / "store") as store:
        sync_drive(store,CONFIG,DriveFixture({"":{"files":[{"id":"old-file"}]}}),10)
        sid,pid=activate_comment(store,"old-file")
        result=sync_drive(store,CONFIG,Client({"":{"files":[{"id":"old-file"}]}}),10)
        assert store.get_source(sid)["status"]=="unavailable"
        assert store.get_principle(pid)["evidence"]==[]
        assert result["access_withdrawn"]==1


def test_successful_drive_attempt_replaces_prior_error_report(tmp_path):
    client=DriveFixture({"":{"files":[]}})
    with opened(tmp_path / "store") as store:
        store.coverage("drive:last_attempt",{"state":"error","error_code":"old_auth_error"})
        with patch("tools.cha_philosophy.sync.GogClient",return_value=client):
            sync(store,CONFIG,"drive",10)
        attempt=json.loads(store.db.execute("SELECT data FROM coverage WHERE scope='drive:last_attempt'").fetchone()[0])
        assert attempt["attempt_succeeded"] is True and "error_code" not in attempt


def test_changed_drive_query_supersedes_old_report_without_revoking_old_scope(tmp_path):
    with opened(tmp_path / "store") as store:
        original=sync_drive(store,{**CONFIG,"drive_query":"name contains 'old'"},DriveFixture({"":{"files":[{"id":"old-file"}]}}),10)
        sid,pid=activate_comment(store,"old-file")
        current=sync_drive(store,CONFIG,DriveFixture({"":{"files":[]}}),10)
        reports={r["scope"]:json.loads(r["data"]) for r in store.db.execute("SELECT scope,data FROM coverage")}
        old=reports[original["sync_scope"]]
        assert old["state"]=="superseded" and old["previous_state"]=="complete_for_query"
        assert old["superseded_by"]==current["sync_scope"]
        assert reports["drive:last_attempt"]["inventory_query"]==current["inventory_query"]
        assert reports["drive:last_attempt"]["scope_status"]=="active"
        assert store.get_source(sid)["status"]=="active"
        assert store.get_principle(pid)["status"]=="evidence_supported"


def add_supported_gmail(store,tid):
    src=next(s for s in store.sources(platform="gmail",direct=True) if s["metadata"]["thread_id"]==tid)
    pid=store.add_principle({"statement":"Use evidence in interpretations.","domains":["review"],
                             "evidence":[{"source_id":src["source_id"],"quote":src["authored_text"],"source_hash":src["source_hash"]}]})
    store.review(pid,"evidence_supported","synthetic reviewer","Exact evidence checked.")
    return src["source_id"],pid


def expire_refresh_time(store,sid):
    old=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat()
    store.db.execute("UPDATE sources SET verified_at=? WHERE source_id=?",(old,sid));store.db.commit()
    return old


def test_supported_gmail_refresh_runs_first_and_deduplicates_threads(tmp_path):
    with opened(tmp_path / "store") as store:
        sync_gmail(store,CONFIG,GmailFixture({"":{"threads":[{"id":"curated"}]}}),10)
        sid,pid=add_supported_gmail(store,"curated")
        add_supported_gmail(store,"curated")
        old=expire_refresh_time(store,sid)
        client=GmailFixture({"":{"threads":[{"id":"backlog-a"},{"id":"backlog-b"}]}})
        result=sync_gmail(store,CONFIG,client,1)
        assert client.thread_calls==["curated","backlog-a"]
        assert result["supported_evidence_refresh"]["due_threads"]==1
        assert store.get_source(sid)["verified_at"]!=old
        assert store.get_principle(pid)["status"]=="evidence_supported"
        again=refresh_supported_evidence(store,CONFIG,client)
        assert again["due_threads"]==0 and client.thread_calls==["curated","backlog-a"]


def test_supported_gmail_changed_or_inaccessible_evidence_is_withdrawn(tmp_path):
    class Changed(GmailFixture):
        def get_gmail_thread(self,tid):
            raw=thread(tid);raw["thread"]["messages"][0]["body"]="The interpretation changed with new evidence."
            return raw
    with opened(tmp_path / "store") as store:
        sync_gmail(store,CONFIG,GmailFixture({"":{"threads":[{"id":"a"},{"id":"b"}]}}),10)
        sid,pid=add_supported_gmail(store,"a")
        denied_sid,denied_pid=add_supported_gmail(store,"b")
        expire_refresh_time(store,sid);expire_refresh_time(store,denied_sid)
        class Client(Changed):
            def get_gmail_thread(self,tid):
                if tid=="b":raise ConnectorError("source_not_found")
                return super().get_gmail_thread(tid)
        result=refresh_supported_evidence(store,CONFIG,Client({}))
        assert store.get_principle(pid)["status"]=="stale"
        assert store.get_source(denied_sid)["status"]=="unavailable"
        assert store.get_principle(denied_pid)["evidence"]==[]
        assert result["withdrawn_sources"]==1


def test_supported_gmail_transient_error_preserves_evidence_and_timestamp(tmp_path):
    with opened(tmp_path / "store") as store:
        sync_gmail(store,CONFIG,GmailFixture({"":{"threads":[{"id":"a"}]}}),10)
        sid,pid=add_supported_gmail(store,"a");old=expire_refresh_time(store,sid)
        result=refresh_supported_evidence(store,CONFIG,GmailFixture({},fail_once={"a"}))
        assert result["state"]=="partial" and len(result["failures"])==1
        assert store.get_source(sid)["verified_at"]==old
        assert store.get_source(sid)["status"]=="active"
        assert store.get_principle(pid)["status"]=="evidence_supported"


class TeamsFixture:
    def __init__(self):
        self.channel_reads = []

    def iter_collection(self, path, **kwargs):
        yield from [{"id": "channel-" + x} for x in ("a", "b", "c")]

    def iter_channel_messages(self, team, cid, **kwargs):
        self.channel_reads.append(cid)
        yield {"id": "message-" + cid, "from": {"user": {"id": CONFIG["teams_professor_ids"][0]}},
               "body": {"contentType": "text", "content": "Evaluate the evidence carefully. " + cid},
               "createdDateTime": "2025-01-01T00:00:00Z", "messageType": "message"}


def test_teams_channel_cap_resumes_instead_of_repeating_first_channel(tmp_path):
    home = tmp_path / "store"
    client = TeamsFixture()
    states, counts = [], []
    with patch("tools.cha_philosophy.sync.TeamsGraphClient.from_env", return_value=client):
        for _ in range(3):
            with opened(home) as store:
                result = sync_teams(store, CONFIG, limit=1)
                states.append(result["state"])
                counts.append(len(list(store.sources(platform="teams", direct=True))))
    assert counts == [1, 2, 3]
    assert states == ["partial", "partial", "complete_configured_channels"]
    assert client.channel_reads == ["channel-a", "channel-b", "channel-c"]


def teams_message(mid,author=None,**extra):
    return {"id":mid,"from":{"user":{"id":author or CONFIG["teams_professor_ids"][0],"displayName":"Professor"}},
            "body":{"contentType":"text","content":"Use independent evidence. "+mid},
            "messageType":"message","createdDateTime":"2025-01-01T00:00:00Z",**extra}


class TeamsChatFixture(TeamsFixture):
    def __init__(self,chat_pages,message_pages,channel_failure=False):
        super().__init__();self.chat_pages=chat_pages;self.message_pages=message_pages
        self.chat_lists=[];self.chat_reads=[];self.channel_failure=channel_failure

    def iter_collection(self,path,**kwargs):
        if self.channel_failure:raise ConnectorError("teams_access_denied")
        yield from super().iter_collection(path,**kwargs)

    def list_user_chats(self,uid,*,progress=None,max_pages=None,start_page=None):
        key=start_page or "";self.chat_lists.append((uid,key))
        page=self.chat_pages[key]
        if isinstance(page,Exception):raise page
        progress.remaining_page_token=key or None;progress.pages=1
        yield from deepcopy(page["value"])
        progress.remaining_page_token=page.get("next")
        progress.complete=not page.get("next")

    def iter_chat_messages(self,cid,*,progress=None,max_pages=None,start_page=None):
        key=start_page or "";self.chat_reads.append((cid,key))
        page=self.message_pages[(cid,key)]
        if isinstance(page,Exception):raise page
        progress.remaining_page_token=key or None;progress.pages=1
        yield from deepcopy(page["value"])
        progress.remaining_page_token=page.get("next")
        progress.complete=not page.get("next")


CHAT_CONFIG={**CONFIG,"teams_include_chats":True,"teams_chat_page_limit":1}


def run_teams_chat(store,client,limit=10,config=None):
    with patch("tools.cha_philosophy.sync.TeamsGraphClient.from_env",return_value=client):
        return sync_teams(store,config or CHAT_CONFIG,limit)


def test_graph_chat_ingestion_is_enabled_explicitly_and_keeps_other_authors_context(tmp_path):
    client=TeamsChatFixture({"":{"value":[{"id":"private-chat"}]}},{("private-chat",""):{"value":[teams_message("prof"),teams_message("student","student-id")]}})
    with opened(tmp_path/"store") as store:
        disabled=run_teams_chat(store,client,config=CONFIG)
        assert disabled["chat_sync"]["state"]=="disabled" and not client.chat_lists
        result=run_teams_chat(store,client)
        assert result["state"]=="complete_configured_channels_and_chats"
        messages={s["native_id"]:s for s in store.sources(platform="teams") if s["scope"]=="teams:private-chat"}
        assert messages["prof"]["authorship"]=="direct" and messages["prof"]["authored_text"]
        assert messages["student"]["authorship"]=="context" and not messages["student"]["authored_text"]
        assert messages["student"]["author_name"]==messages["prof"]["author_name"]
        assert messages["prof"]["metadata"]["container_type"]=="chat"


def test_chat_container_cap_survives_restart_without_rereading_completed_chats(tmp_path):
    client=TeamsChatFixture({"":{"value":[{"id":c} for c in ("chat-a","chat-b","chat-c")]}},
                            {(c,""):{"value":[teams_message("message-"+c)]} for c in ("chat-a","chat-b","chat-c")})
    results=[]
    for _ in range(3):
        with opened(tmp_path/"store") as store:results.append(run_teams_chat(store,client,1))
    assert [r["chat_sync"]["state"] for r in results]==["partial","partial","complete_configured_chats"]
    assert client.chat_reads==[("chat-a",""),("chat-b",""),("chat-c","")]
    assert len(client.chat_lists)==1


def test_channel_listing_failure_does_not_prevent_chat_ingestion(tmp_path):
    client=TeamsChatFixture({"":{"value":[{"id":"chat"}]}},{("chat",""):{"value":[teams_message("p")]}},channel_failure=True)
    with opened(tmp_path/"store") as store:
        result=run_teams_chat(store,client)
        assert result["channel_sync"]["state"]=="error"
        assert result["chat_sync"]["state"]=="complete_configured_chats"
        assert result["state"]=="partial"
        assert any(s["scope"]=="teams:chat" for s in store.sources())


def test_chat_list_failure_does_not_prevent_channel_ingestion_or_revoke_old_chat(tmp_path):
    with opened(tmp_path/"store") as store:
        run_teams_chat(store,TeamsChatFixture({"":{"value":[{"id":"chat"}]}},{("chat",""):{"value":[teams_message("p")]}}))
        old=next(s for s in store.sources() if s["scope"]=="teams:chat")
        result=run_teams_chat(store,TeamsChatFixture({"":ConnectorError("teams_access_denied")},{}))
        assert result["channel_sync"]["state"]=="complete_configured_channels"
        assert result["chat_sync"]["state"]=="error" and result["state"]=="partial"
        assert store.get_source(old["source_id"])["status"]=="active"


def test_partial_chat_list_can_ingest_seen_chat_and_never_revoke_unseen_chat(tmp_path):
    with opened(tmp_path/"store") as store:
        run_teams_chat(store,TeamsChatFixture({"":{"value":[{"id":"old"}]}},{("old",""):{"value":[teams_message("old-message")]}}))
        old=next(s for s in store.sources() if s["scope"]=="teams:old")
        client=TeamsChatFixture({"":{"value":[{"id":"new"}],"next":"inventory-page2"},"inventory-page2":{"value":[{"id":"old"}]}},
                               {("new",""):{"value":[teams_message("new-message")]},("old",""):{"value":[teams_message("old-message")]}})
        first=run_teams_chat(store,client)
        assert first["chat_sync"]["inventory_complete"] is False
        assert store.get_source(old["source_id"])["status"]=="active"
    with opened(tmp_path/"store") as store:
        final=run_teams_chat(store,client)
        assert final["chat_sync"]["state"]=="complete_configured_chats"
        assert store.get_source(old["source_id"])["status"]=="active"
    assert client.chat_lists[-1][1]=="inventory-page2"


def test_chat_message_page_resume_withdraws_missing_message_only_after_full_read(tmp_path):
    initial=TeamsChatFixture({"":{"value":[{"id":"chat"}]}},{("chat",""):{"value":[teams_message("keep"),teams_message("delete"),teams_message("later")]}})
    with opened(tmp_path/"store") as store:
        run_teams_chat(store,initial)
        deleted=next(s for s in store.sources() if s["native_id"]=="delete")
        client=TeamsChatFixture({"":{"value":[{"id":"chat"}]}},
                               {("chat",""):{"value":[teams_message("keep")],"next":"message-page2"},("chat","message-page2"):{"value":[teams_message("later")]}})
        first=run_teams_chat(store,client)
        assert first["chat_sync"]["state"]=="partial"
        assert store.get_source(deleted["source_id"])["status"]=="active"
    with opened(tmp_path/"store") as store:
        final=run_teams_chat(store,client)
        assert final["chat_sync"]["state"]=="complete_configured_chats"
        assert store.get_source(deleted["source_id"])["status"]=="unavailable"
        assert len([s for s in store.sources() if s["scope"]=="teams:chat" and s["status"]=="active"])==2
    assert client.chat_reads==[("chat",""),("chat","message-page2")]


def test_complete_empty_chat_inventory_withdraws_whole_chat_but_not_channels(tmp_path):
    with opened(tmp_path/"store") as store:
        run_teams_chat(store,TeamsChatFixture({"":{"value":[{"id":"chat"}]}},{("chat",""):{"value":[teams_message("p")]}}))
        chat=next(s for s in store.sources() if s["scope"]=="teams:chat")
        channel=next(s for s in store.sources() if s["scope"]=="teams:channel-a")
        result=run_teams_chat(store,TeamsChatFixture({"":{"value":[]}},{}))
        assert result["chat_sync"]["state"]=="complete_configured_chats"
        assert store.get_source(chat["source_id"])["status"]=="unavailable"
        assert store.get_source(channel["source_id"])["status"]=="active"


def test_one_failed_chat_does_not_block_other_chat_or_erase_failed_chat(tmp_path):
    with opened(tmp_path/"store") as store:
        run_teams_chat(store,TeamsChatFixture({"":{"value":[{"id":"a"}]}},{("a",""):{"value":[teams_message("old")]}}))
        old=next(s for s in store.sources() if s["scope"]=="teams:a")
        client=TeamsChatFixture({"":{"value":[{"id":"a"},{"id":"b"}]}},
                               {("a",""):ConnectorError("graph_network_failed"),("b",""):{"value":[teams_message("new")]}})
        result=run_teams_chat(store,client)
        assert result["chat_sync"]["state"]=="partial"
        assert result["chat_sync"]["pending_chats"]==1 and result["chat_sync"]["chats"]==1
        assert store.get_source(old["source_id"])["status"]=="active"
        assert client.chat_reads==[("a",""),("b","")]


def test_failed_first_chat_rotates_with_one_container_budget(tmp_path):
    client=TeamsChatFixture({"":{"value":[{"id":"a"},{"id":"b"}]}},
                           {("a",""):ConnectorError("teams_access_denied"),("b",""):{"value":[teams_message("new")]}})
    with opened(tmp_path/"store") as store:
        first=run_teams_chat(store,client,1)
        assert first["chat_sync"]["state"]=="error"
    with opened(tmp_path/"store") as store:
        second=run_teams_chat(store,client,1)
        assert second["chat_sync"]["chats"]==1
        assert any(s["scope"]=="teams:b" for s in store.sources())
    assert client.chat_reads==[("a",""),("b","")]


def test_failed_resumed_chat_page_keeps_its_exact_cursor(tmp_path):
    client=TeamsChatFixture({"":{"value":[{"id":"chat"}]}},
                           {("chat",""):{"value":[teams_message("one")],"next":"page2"},
                            ("chat","page2"):ConnectorError("graph_network_failed")})
    with opened(tmp_path/"store") as store:
        run_teams_chat(store,client,1)
        failed=run_teams_chat(store,client,1)
        assert failed["chat_sync"]["state"]=="error"
    client.message_pages[("chat","page2")]={"value":[teams_message("two")]}
    with opened(tmp_path/"store") as store:
        final=run_teams_chat(store,client,1)
        assert final["chat_sync"]["state"]=="complete_configured_chats"
        assert len([s for s in store.sources() if s["scope"]=="teams:chat"])==2
    assert client.chat_reads==[("chat",""),("chat","page2"),("chat","page2")]


def test_real_graph_adapter_resumes_verified_chat_pages_into_store(tmp_path):
    requests=[]
    base=TeamsGraphClient.BASE
    user=CONFIG["teams_professor_ids"][0]
    chat="chat-with-pages"
    final=base+"/chats/"+chat+"/messages?$skiptoken=second"
    pages={base+"/teams/synthetic-team/channels":{"value":[]},
           base+"/users/"+user+"/chats?$top=50":{"value":[{"id":chat,"chatType":"oneOnOne"}]},
           base+"/chats/"+chat+"/messages?$top=50":{"value":[teams_message("prof-first")],"@odata.nextLink":final},
           final:{"value":[teams_message("context-last","student-id")]}}
    class Opener:
        def open(self,request,timeout=None):
            requests.append(request.full_url)
            assert request.method=="GET" and request.data is None
            return io.BytesIO(json.dumps(pages[request.full_url]).encode())
    client=TeamsGraphClient("SYNTHETIC_TOKEN",opener=Opener())
    with opened(tmp_path/"store") as store:
        first=run_teams_chat(store,client,1)
        assert first["chat_sync"]["state"]=="partial"
    with opened(tmp_path/"store") as store:
        second=run_teams_chat(store,client,1)
        assert second["state"]=="complete_configured_channels_and_chats"
        rows=list(store.sources(platform="teams"))
        assert len(rows)==2
        assert {row["authorship"] for row in rows}=={"direct","context"}
        assert all(row["verified_at"] for row in rows)
    assert requests.count(base+"/users/"+user+"/chats?$top=50")==1
    assert requests.count(base+"/chats/"+chat+"/messages?$top=50")==1
    assert requests.count(final)==1


def test_complete_live_chat_reconciles_native_snapshot_only_with_exact_scope(tmp_path):
    raw={"message_id":"native-only","chat_id":"chat","author_user_id":CONFIG["teams_professor_ids"][0],
         "content":"Retain independent reasoning.","author_name":"Professor"}
    with opened(tmp_path/"store") as store:
        exact=normalize_teams_native(raw,CONFIG["teams_tenant_id"],set(CONFIG["teams_professor_ids"]))
        exact["metadata"]["sync_scope"]="native-snapshot"
        other=normalize_teams_native(raw,"other-tenant",set(CONFIG["teams_professor_ids"]))
        unknown=normalize_teams_native({**raw,"message_id":"unknown-container"},CONFIG["teams_tenant_id"],set(CONFIG["teams_professor_ids"]))
        unknown["metadata"].pop("container_type")
        for source in (exact,other,unknown):store.upsert(source,verified_remote=True)
        result=run_teams_chat(store,TeamsChatFixture({"":{"value":[{"id":"chat"}]}},{("chat",""):{"value":[]}}))
        assert result["chat_sync"]["state"]=="complete_configured_chats"
        assert store.get_source(exact["source_id"])["status"]=="unavailable"
        assert store.get_source(other["source_id"])["status"]=="active"
        assert store.get_source(unknown["source_id"])["status"]=="active"
