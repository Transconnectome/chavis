"""Priority Drive evidence refresh uses complete remote reads and no body crawl."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import json

import pytest

from tools.cha_philosophy.connectors import ConnectorError,normalize_drive_comment,normalize_drive_document
from tools.cha_philosophy.refresh import _source_summary
from tools.cha_philosophy.store import Store,digest
from tools.cha_philosophy.sync import refresh_supported_drive_evidence,sync_drive,has_operation_errors

CONFIG={"account":"professor@example.invalid","drive_account_is_professor":True,"drive_query":"synthetic query"}


@pytest.fixture
def store(tmp_path):
    value=Store(tmp_path/"private")
    yield value
    value.close()


def seeded(store):
    account=CONFIG["account"]
    scope="drive:"+account+":"+digest(CONFIG["drive_query"])[:12]
    meta={"id":"file","name":"Synthetic file","mimeType":"application/vnd.google-apps.document","_account_id":account}
    comment={"id":"comment","author":{"me":True},"content":"Claims must match the observed evidence.",
             "createdTime":"2026-01-01T00:00:00Z","replies":[
                 {"id":"reply","author":{"me":True},"content":"State the scope and exceptions explicitly.","createdTime":"2026-01-02T00:00:00Z"}]}
    rows=normalize_drive_comment(meta,comment,True)
    body=normalize_drive_document(meta,{"extracted_text":"Synthetic shared body.","extraction":{"format":"synthetic"}})
    for row in rows+[body]:
        row["metadata"]["sync_scope"]=scope
        store.upsert(row,verified_remote=True)
    evidence=[]
    for row in rows:
        current=store.get_source(row["source_id"])
        evidence.append({"source_id":current["source_id"],"source_hash":current["source_hash"],"quote":current["authored_text"]})
    pid=store.add_principle({"statement":"Bound claims to evidence.","domains":["review"],"evidence":evidence})
    store.review(pid,"evidence_supported","synthetic-reviewer","Synthetic fixture.")
    old=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat()
    with store.transaction():
        store.db.execute("UPDATE sources SET verified_at=?",(old,))
    checkpoint={"in_progress":True,"pending":["unrelated-file"],"seen_ids":[],"page_token":"PRIVATE_CURSOR",
                "page_loaded":True,"files":29,"pages":1,"unsupported":0,"content_gaps":{},
                "extraction_limitations":[],"page_tokens_seen":[],"access_withdrawn":0}
    store.checkpoint(scope,checkpoint)
    return meta,comment,rows,body,pid,scope


class Client:
    account=CONFIG["account"]
    def __init__(self,meta,comments,*,error=None,stage="comments",complete=True):
        self.meta,self.comments=deepcopy(meta),deepcopy(comments)
        self.error,self.stage,self.complete=error,stage,complete
        self.metadata_calls=[];self.comment_calls=[]
    def get_drive_metadata(self,fid):
        self.metadata_calls.append(fid)
        if self.error and self.stage=="metadata":raise ConnectorError(self.error)
        return deepcopy(self.meta)
    def iter_drive_comments(self,fid,*,progress=None):
        self.comment_calls.append(fid)
        for comment in self.comments:yield deepcopy(comment)
        if self.error:raise ConnectorError(self.error)
        progress.complete=self.complete
    def read_document(self,*args):raise AssertionError("priority refresh must not read a body")
    def drive_page(self,*args):raise AssertionError("priority refresh must not change inventory")


def test_due_comment_and_reply_refresh_once_per_file_without_new_sources_or_body_writes(store):
    meta,comment,rows,body,pid,scope=seeded(store)
    held_comment={"id":"held-comment","author":{"me":True},"content":"Held-out reference feedback must remain separate.","replies":[]}
    held=normalize_drive_comment(meta,held_comment,True)[0]
    store.upsert(held,verified_remote=True)
    store.hold_for_evaluation(held["source_id"],"Reserve the independent feedback.")
    new_comment={"id":"new-comment","author":{"me":True},"content":"New comment not part of existing principle evidence.","replies":[]}
    before_body=store.get_source(body["source_id"])
    before_held=store.get_source(held["source_id"])
    checkpoint=list(store.db.execute("SELECT * FROM checkpoints"))
    before_hashes={row["source_id"]:store.get_source(row["source_id"])["source_hash"] for row in rows}
    client=Client(meta,[comment,held_comment,new_comment])
    result=refresh_supported_drive_evidence(store,CONFIG,client)
    assert result["due_files"]==result["refreshed_files"]==1
    assert result["changed_sources"]==0 and not result["failures"]
    assert client.metadata_calls==client.comment_calls==["file"]
    for row in rows:
        current=store.get_source(row["source_id"])
        assert current["verified_at"]>before_body["verified_at"]
        assert current["source_hash"]==before_hashes[row["source_id"]]
        assert current["metadata"]["sync_scope"]==scope
    assert store.get_source(body["source_id"])==before_body
    assert store.get_source(held["source_id"])==before_held
    assert store.db.execute("SELECT count(*) FROM sources").fetchone()[0]==4
    assert list(store.db.execute("SELECT * FROM checkpoints"))==checkpoint
    assert store.get_principle(pid)["status"]=="evidence_supported"
    again=refresh_supported_drive_evidence(store,CONFIG,client)
    assert again["due_files"]==0 and again["sources_not_due"]==2
    assert client.comment_calls==["file"]


@pytest.mark.parametrize("error",["gog_auth_required","gog_rate_limited","gog_read_failed",None])
def test_partial_or_failed_comment_pages_never_refresh_yielded_sources(store,error):
    meta,comment,rows,body,_,scope=seeded(store)
    before={row["source_id"]:store.get_source(row["source_id"]) for row in rows+[body]}
    checkpoint=store.checkpoint(scope)
    result=refresh_supported_drive_evidence(store,CONFIG,Client(meta,[comment],error=error,complete=False))
    assert result["state"]=="partial" and result["refreshed_files"]==0
    assert result["failures"][0]["error_code"]==(error or "drive_comments_partial")
    assert all(store.get_source(sid)==value for sid,value in before.items())
    assert store.checkpoint(scope)==checkpoint


def test_complete_comments_reconcile_missing_reply_and_changed_authored_text(store):
    meta,comment,rows,body,pid,_=seeded(store)
    before_body=store.get_source(body["source_id"])
    comment["content"]="A materially changed statement requires a new interpretation."
    comment["replies"]=[]
    result=refresh_supported_drive_evidence(store,CONFIG,Client(meta,[comment]))
    assert result["changed_sources"]==1 and result["withdrawn_sources"]==1
    assert store.get_source(rows[0]["source_id"])["authored_text"]==comment["content"]
    assert store.get_source(rows[1]["source_id"])["status"]=="unavailable"
    assert store.get_principle(pid)["status"]=="stale"
    assert store.get_source(body["source_id"])==before_body


@pytest.mark.parametrize("stage",["metadata","comments"])
def test_permission_loss_withdraws_only_the_proven_scope(store,stage):
    meta,comment,rows,body,pid,_=seeded(store)
    before_body=store.get_source(body["source_id"])
    result=refresh_supported_drive_evidence(store,CONFIG,Client(meta,[],error="source_access_denied",stage=stage))
    assert not result["failures"] and result["withdrawn_sources"]==(3 if stage=="metadata" else 2)
    assert all(store.get_source(row["source_id"])["status"]=="unavailable" for row in rows)
    current_body=store.get_source(body["source_id"])
    assert current_body["verified_at"]==before_body["verified_at"]
    assert current_body["status"]==("unavailable" if stage=="metadata" else "active")
    assert store.get_principle(pid)["status"]=="stale"


@pytest.mark.parametrize("mismatch",["account","metadata","trashed","authorship"])
def test_current_remote_identity_and_authorship_are_not_assumed(store,mismatch):
    meta,comment,rows,_,pid,_=seeded(store)
    before=store.get_source(rows[0]["source_id"])
    client=Client(meta,[comment])
    if mismatch=="account":client.account="other@example.invalid"
    elif mismatch=="metadata":client.meta["_account_id"]="other@example.invalid"
    elif mismatch=="trashed":client.meta["trashed"]=True
    else:client.comments[0]["author"]["me"]=False
    result=refresh_supported_drive_evidence(store,CONFIG,client)
    current=store.get_source(rows[0]["source_id"])
    if mismatch in {"account","metadata"}:
        assert result["failures"] and current==before
    else:
        assert store.get_principle(pid)["status"]=="stale"
        assert current["status"]=="unavailable" if mismatch=="trashed" else current["authorship"]=="unverified"


def test_sync_drive_reports_failed_priority_refresh_in_child_and_recurring_summary(store):
    meta,comment,_,_,_,scope=seeded(store)
    checkpoint=store.checkpoint(scope)
    result=sync_drive(store,CONFIG,Client(meta,[comment],error="gog_auth_required"),0)
    assert result["supported_evidence_refresh"]["due_files"]==1
    assert has_operation_errors(result)
    summary=_source_summary("drive",{"results":[result]})
    assert summary["has_errors"] and summary["attempt_succeeded"] is False
    assert summary["supported_evidence_refresh"]["due_files"]==1
    assert summary["supported_evidence_refresh"]["failure_codes"]=={"gog_auth_required":1}
    assert "file_id" not in json.dumps(summary)
    report=json.loads(store.db.execute("SELECT data FROM coverage WHERE scope='drive:last_attempt'").fetchone()[0])
    assert report["attempt_succeeded"] is False
    assert store.checkpoint(scope)==checkpoint
