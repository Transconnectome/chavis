"""Resumable, read-only synchronization. Partial runs never imply completeness."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import html
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .connectors import (ConnectorError, GogClient, SyncProgress, TeamsGraphClient,
                         READABLE_DRIVE_MIME_TYPES,
                         normalize_drive_comment, normalize_drive_document, normalize_drive_document_views,
                         normalize_gmail, normalize_teams, source_key, validate_lab_project_routes)
from .store import ACTIVE, digest, now


def _gmail_query(config):
    identities = config["identities"]
    recipients = config["lab_recipients"]
    routes = validate_lab_project_routes(config.get("lab_project_routes"))
    if not identities or not (recipients or routes):
        raise ValueError("verified identities and lab recipient scope are required")
    recipients = [*recipients, *(address for address in routes if address not in {x.lower() for x in recipients})]
    return "in:sent {" + " ".join("from:"+x for x in identities) + "} {" + " ".join(
        field+":"+x for x in recipients for field in ("to","cc","bcc")) + "}"


def refresh_supported_evidence(store, config, client):
    """Refresh due Gmail evidence independently of a long historical backfill.

    Read only exact threads already supporting active principles. Each source is
    due after one day, leaving margin inside the bundle's 14-day freshness gate.
    Failed reads never receive a new verification timestamp.
    """
    account=config["account"]
    scope="gmail:"+account+":"+digest(_gmail_query(config))[:12]
    source_ids={e["source_id"] for p in store.principles() if p.get("status") in ACTIVE for e in p.get("evidence",[])}
    threads=set();skipped=0
    for sid in sorted(source_ids):
        source=store.get_source(sid)
        if not source or source.get("platform")!="gmail" or source.get("status")!="active": continue
        meta=source.get("metadata",{})
        if meta.get("account_id")!=account or not meta.get("thread_id") or source.get("authorship")!="direct": continue
        try:
            age=(datetime.now(timezone.utc)-datetime.fromisoformat(source["verified_at"])).total_seconds()
        except (KeyError,ValueError,TypeError):
            age=float("inf")
        if 0 <= age < 86400:
            skipped+=1;continue
        threads.add(meta["thread_id"])
    refreshed=changed=withdrawn=0;failures=[]
    for tid in sorted(threads):
        existing=[s for s in store.sources(platform="gmail") if s.get("metadata",{}).get("account_id")==account and s.get("metadata",{}).get("thread_id")==tid]
        try:
            raw=client.get_gmail_thread(tid)
            rows=normalize_gmail(raw,set(config["identities"]),set(config["lab_recipients"]),
                                 lab_project_routes=config.get("lab_project_routes"))
        except ConnectorError as exc:
            if exc.code in {"source_access_denied","source_not_found"}:
                for old in existing:
                    store.revoke(old["source_id"]);withdrawn+=1
            else:
                failures.append({"thread_id":tid,"error_code":exc.code})
            continue
        seen=set()
        for source in rows:
            source["metadata"]["sync_scope"]=scope
            source["metadata"]["sent_vs_authored"]="account emission verified; individual wording or AI assistance not established"
            changed+=store.upsert(source,verified_remote=True);seen.add(source["source_id"])
        for old in existing:
            if old["source_id"] not in seen:
                store.revoke(old["source_id"]);withdrawn+=1
        refreshed+=1
    result={"platform":"gmail","state":"partial" if failures else "complete_due_supported_evidence",
            "due_threads":len(threads),"refreshed_threads":refreshed,"sources_not_due":skipped,
            "changed_sources":changed,"withdrawn_sources":withdrawn,"failures":failures,"refresh_interval_hours":24,
            "limitations":["exact previously evidenced threads only; historical mailbox discovery runs separately"]}
    store.coverage("gmail:supported_evidence_refresh",result)
    return result


def sync_gmail(store, config, client, limit):
    base_query = _gmail_query(config)
    scope = "gmail:" + config["account"] + ":" + digest(base_query)[:12]
    # Scope changes replace the account's current coverage report, not its
    # source history or resumable checkpoints. Keep query ordering unchanged so
    # existing checkpoint hashes remain compatible with the configured lists.
    prefix = "gmail:" + config["account"] + ":"
    for prior in store.db.execute("SELECT scope,data FROM coverage").fetchall():
        prior_scope = prior["scope"]
        if not prior_scope.startswith(prefix) or prior_scope == scope:
            continue
        query_hash = prior_scope[len(prefix):]
        if len(query_hash) != 12 or any(char not in "0123456789abcdef" for char in query_hash):
            continue  # Account-specific auxiliary reports are not query scopes.
        old = json.loads(prior["data"])
        if old.get("scope_status") != "superseded" or old.get("superseded_by") != scope:
            old.update(previous_state=old.get("previous_state", old.get("state")), state="superseded",
                       scope_status="superseded", superseded_by=scope, superseded_at=now())
            store.coverage(prior_scope, old)
    evidence_refresh=refresh_supported_evidence(store,config,client)
    state = store.checkpoint(scope) or {}
    if not state.get("in_progress"):
        last_full = state.get("last_full_started")
        full = not last_full or (datetime.now(timezone.utc)-datetime.fromisoformat(last_full)).days >= 7
        query = base_query
        if not full and state.get("last_started"):
            after = datetime.fromisoformat(state["last_started"]) - timedelta(days=3)
            query += " after:" + str(int(after.timestamp()))
        state = {**state, "in_progress":True,"started":now(),"full":full,"query":query,
                 "pending":[],"page_token":"","page_loaded":False,"seen":[],"threads":0,"pages":0}
        store.checkpoint(scope,state)
    processed=changed=0
    while processed < limit:
        if not state["pending"]:
            if state["page_loaded"] and not state["page_token"]:
                break
            page=client.gmail_page(state["query"],state["page_token"])
            if "threads" not in page:
                raise ConnectorError("gmail_search_shape")
            state["pending"]=[x["id"] for x in page.get("threads",[])]
            state["page_token"]=page.get("nextPageToken") or ""
            state["page_loaded"]=True; state["pages"]+=1
            store.checkpoint(scope,state)
            if not state["pending"] and not state["page_token"]:
                break
        if not state["pending"]:
            continue
        tid=state["pending"][0]
        raw=client.get_gmail_thread(tid)
        rows=normalize_gmail(raw,set(config["identities"]),set(config["lab_recipients"]),
                             lab_project_routes=config.get("lab_project_routes"))
        for source in rows:
            source["metadata"]["sync_scope"]=scope
            source["metadata"]["sent_vs_authored"]="account emission verified; individual wording or AI assistance not established"
            changed += store.upsert(source,verified_remote=True)
            if state["full"]: state["seen"].append(source["source_id"])
        state["pending"].pop(0); state["threads"]+=1; processed+=1
        store.checkpoint(scope,state)
    complete=state["page_loaded"] and not state["pending"] and not state["page_token"]
    if complete:
        if state["full"]:
            seen=set(state["seen"])
            for old in list(store.sources(platform="gmail")):
                if old.get("metadata",{}).get("sync_scope")==scope and old["source_id"] not in seen:
                    store.revoke(old["source_id"])
            state["last_full_started"]=state["started"]
        state.update(in_progress=False,last_started=state["started"],seen=[])
        store.checkpoint(scope,state)
    result={"platform":"gmail","state":"complete_for_query" if complete else "partial",
            "sync_scope":scope,"scope_status":"active","inventory_query":base_query,"cycle_query":state["query"],
            "processed_threads":processed,"changed_sources":changed,"cycle_threads":state["threads"],
            "pages":state["pages"],"pending_threads":len(state["pending"]),"more_pages":bool(state["page_token"]),
            "cycle_kind":"full_reconciliation" if state["full"] else "overlap_poll",
            "supported_evidence_refresh":evidence_refresh,
            "limitations":["recipient roster is observed, not a complete historical membership register",
                           "full query enumeration does not establish all historical mailbox content",
                           "Google history API not wired; overlap poll plus weekly full query reconciliation"]}
    store.coverage(scope,result)
    return result


def _discard_drive_document_cache(client, file_id):
    # Test/legacy adapters may have no resumable body cache. The live adapter
    # scopes invalidation to this authenticated account and file only.
    discard=getattr(client,"discard_document_cache",None)
    if callable(discard):return discard(file_id)


def refresh_supported_drive_evidence(store, config, client):
    """Recheck due direct Drive comment/reply evidence without reading file bodies.

    Stage the complete comment pagination before renewing any verification time.
    Only existing active-principle sources are refreshed; discovery, held sources
    and the main inventory checkpoint belong to separate workflows.
    """
    account=config["account"]
    source_ids={e["source_id"] for p in store.principles() if p.get("status") in ACTIVE
                for e in p.get("evidence",[])}
    files={}; due=set(); skipped=0
    for sid in sorted(source_ids):
        source=store.get_source(sid)
        if (not source or source.get("platform")!="drive" or source.get("status")!="active"
                or source.get("authorship")!="direct" or source.get("author_id")!=account):
            continue
        metadata=source.get("metadata",{})
        fid=metadata.get("file_id")
        if (metadata.get("account_id")!=account or not isinstance(fid,str) or not fid
                or metadata.get("source_kind") not in {"comment","reply"}
                or store.evaluation_held(source)):
            continue
        files.setdefault(fid,{})[sid]=source
        try:
            age=(datetime.now(timezone.utc)-datetime.fromisoformat(source["verified_at"])).total_seconds()
        except (KeyError,TypeError,ValueError):
            age=float("inf")
        if 0 <= age < 86400: skipped+=1
        else: due.add(fid)
    refreshed=changed=withdrawn=0; failures=[]
    for fid in sorted(due):
        targets=files[fid]; stage="metadata"
        try:
            if getattr(client,"account",account)!=account or config.get("drive_account_is_professor") is not True:
                raise ConnectorError("drive_refresh_account_unverified")
            meta=client.get_drive_metadata(fid)
            if (not isinstance(meta,dict) or meta.get("id")!=fid
                    or meta.get("_account_id",account)!=account or meta.get("account_id",account)!=account):
                raise ConnectorError("drive_refresh_metadata_identity_mismatch")
            if meta.get("trashed"):
                raise ConnectorError("source_not_found")
            meta={**meta,"_account_id":account}
            stage="comments"; progress=SyncProgress(); observed={}
            for comment in client.iter_drive_comments(fid,progress=progress):
                for source in normalize_drive_comment(meta,comment,True):
                    sid=source["source_id"]
                    if sid not in targets: continue
                    if sid in observed:
                        raise ConnectorError("drive_refresh_duplicate_source")
                    observed[sid]=source
            if not progress.complete:
                raise ConnectorError("drive_comments_partial")
            file_changed=file_withdrawn=0
            with store.transaction():
                # A concurrent revocation/edit/hold must not be undone by this
                # earlier remote snapshot. No write occurs until all checks pass.
                for sid,old in targets.items():
                    current=store.get_source(sid)
                    if (not current or current["status"]!="active"
                            or current["source_hash"]!=old["source_hash"] or store.evaluation_held(current)):
                        raise ConnectorError("drive_refresh_registry_changed")
                for sid,old in targets.items():
                    source=observed.get(sid)
                    if source is None or source["status"]!="active":
                        store.revoke(sid); file_withdrawn+=1
                        continue
                    if old.get("metadata",{}).get("sync_scope"):
                        source["metadata"]["sync_scope"]=old["metadata"]["sync_scope"]
                    file_changed+=store.upsert(source,verified_remote=True)
            changed+=file_changed; withdrawn+=file_withdrawn; refreshed+=1
        except Exception as exc:
            code=exc.code if isinstance(exc,ConnectorError) else "drive_supported_refresh_failed"
            if code in {"source_access_denied","source_not_found"}:
                # Metadata denial withdraws the file; a comments denial only
                # withdraws comment/reply sources. Body timestamps are not renewed.
                with store.transaction():
                    for old in list(store.sources(platform="drive", scope="drive:"+fid)):
                        metadata=old.get("metadata",{})
                        if (metadata.get("account_id")==account and metadata.get("file_id")==fid
                                and (stage=="metadata" or metadata.get("source_kind") in {"comment","reply"})):
                            store.revoke(old["source_id"]); withdrawn+=1
                if stage=="metadata":_discard_drive_document_cache(client,fid)
            else:
                failures.append({"file_id":fid,"stage":stage,"error_code":code})
    result={"platform":"drive","state":"partial" if failures else "complete_due_supported_evidence",
            "due_files":len(due),"refreshed_files":refreshed,"sources_not_due":skipped,
            "changed_sources":changed,"withdrawn_sources":withdrawn,"failures":failures,
            "refresh_interval_hours":24,
            "limitations":["existing active-principle direct comments/replies only; no new-source discovery",
                           "file bodies and inventory checkpoints are not refreshed by this operation"]}
    store.coverage("drive:supported_evidence_refresh",result)
    return result


def sync_drive(store, config, client, limit):
    evidence_refresh=refresh_supported_drive_evidence(store,config,client)
    # Inventory is persisted independently from document interpretation. Explicit IDs
    # are always included; discovery queries can expand without replacing prior work.
    query=config.get("drive_query") or "trashed = false and (" + " or ".join(
        "mimeType = '" + mime + "'" for mime in sorted(READABLE_DRIVE_MIME_TYPES)) + ")"
    scope="drive:"+config["account"]+":"+digest(query)[:12]
    # Keep earlier query reports as history, while exposing one current account
    # inventory. Changing the query never revokes sources from the older scope.
    prefix="drive:"+config["account"]+":"
    for prior in store.db.execute("SELECT scope,data FROM coverage").fetchall():
        if prior["scope"]!=scope and prior["scope"].startswith(prefix):
            old=json.loads(prior["data"])
            if old.get("scope_status")!="superseded" or old.get("superseded_by")!=scope:
                old.update(previous_state=old.get("previous_state",old.get("state")),state="superseded",
                           scope_status="superseded",superseded_by=scope,superseded_at=now())
                store.coverage(prior["scope"],old)
    state=store.checkpoint(scope) or {}
    if not state.get("in_progress"):
        state={"in_progress":True,"started":now(),"pending":list(config.get("drive_file_ids",[])),
               "seen_ids":[],"page_token":"","page_loaded":False,"files":0,"pages":0,"unsupported":0,
               "content_gaps":{},"extraction_limitations":[],"page_tokens_seen":[],"access_withdrawn":0}
        store.checkpoint(scope,state)
    for key, default in (("content_gaps",{}),("extraction_limitations",[]),("page_tokens_seen",[]),("access_withdrawn",0)):
        state.setdefault(key,default)
    processed=changed=0
    while processed < limit:
        if not state["pending"]:
            if state["page_loaded"] and not state["page_token"]: break
            if state["page_token"] in state["page_tokens_seen"]:
                raise ConnectorError("drive_pagination_cycle")
            page=client.drive_page(query,state["page_token"])
            if not isinstance(page.get("files"),list): raise ConnectorError("drive_inventory_shape")
            if page.get("incompleteSearch"):
                raise ConnectorError("drive_incomplete_search")
            next_page=page.get("nextPageToken") or ""
            if not isinstance(next_page,str): raise ConnectorError("drive_page_token_invalid")
            state["page_tokens_seen"].append(state["page_token"])
            state["pending"]=[x["id"] for x in page["files"] if x["id"] not in state["seen_ids"]]
            state["page_token"]=next_page;state["page_loaded"]=True;state["pages"]+=1
            store.checkpoint(scope,state)
            if not state["pending"] and not state["page_token"]: break
        if not state["pending"]: continue
        fid=state["pending"][0]
        try:
            meta=client.get_drive_metadata(fid)
        except ConnectorError as exc:
            if exc.code not in {"source_access_denied","source_not_found"}: raise
            for old in list(store.sources(platform="drive", scope="drive:"+fid)):
                if old["scope"]=="drive:"+fid: store.revoke(old["source_id"])
            _discard_drive_document_cache(client,fid)
            state["access_withdrawn"]+=1
            state["content_gaps"][fid]={"file_id":fid,"reason":exc.code,"stage":"metadata"}
            state["pending"].pop(0);state["files"]+=1;processed+=1
            store.checkpoint(scope,state)
            continue
        meta["_account_id"]=config["account"]
        seen=set()
        if meta.get("trashed"):
            for old in list(store.sources(platform="drive", scope="drive:"+fid)):
                if old["scope"]=="drive:"+fid: store.revoke(old["source_id"])
            _discard_drive_document_cache(client,fid)
        else:
            document_read=False
            try:
                if meta.get("mimeType") not in READABLE_DRIVE_MIME_TYPES:
                    raise ConnectorError("document_format_unsupported")
                document=client.read_document(meta)
                documents=normalize_drive_document_views(meta,document)
                with store.transaction():
                    for source in documents:
                        source["metadata"]["sync_scope"]=scope
                        changed+=store.upsert(source,verified_remote=True);seen.add(source["source_id"])
                    if len(documents)==1:
                        # A default read does not re-read the former INLINE view.
                        # Keep its old body/verification time and make the stale
                        # or unpaired relationship visible to context consumers.
                        for old in list(store.sources(platform="drive", scope="drive:"+fid)):
                            previous=old.get("metadata",{})
                            if (old["scope"]!="drive:"+fid
                                    or previous.get("view_role")!="suggestions_inline_context"):
                                continue
                            current=documents[0]
                            old_revision=previous.get("native_revision")
                            new_revision=current["metadata"].get("native_revision")
                            changed_revision=(old.get("modified_at")!=current.get("modified_at")
                                or bool(old_revision and new_revision and old_revision!=new_revision))
                            previous["paired_view_status"]=("stale_for_canonical_revision" if changed_revision
                                                            else "not_rechecked_with_canonical")
                            store.upsert(old,verified_remote=False);seen.add(old["source_id"])
                document_read=True
                state["content_gaps"].pop(fid,None)
                extraction=documents[0]["metadata"].get("extraction",{})
                if extraction.get("partial_text"):
                    state["content_gaps"][fid]={"file_id":fid,"mime_type":meta.get("mimeType", ""),
                        "reason":"document_partial_text_extraction","stage":"document",
                        "fallback_reason":extraction.get("fallback_reason", ""),"limitations":extraction.get("limitations",[])}
                state["extraction_limitations"]=sorted(set(state["extraction_limitations"]) | set(
                    extraction.get("limitations",[])))
            except ConnectorError as exc:
                if getattr(exc,"file_access_withdrawn",False):
                    # A final Drive metadata read can establish file-wide loss
                    # after its initial metadata was readable. View-only Docs
                    # denial never sets this flag and preserves prior bodies.
                    for old in list(store.sources(platform="drive", scope="drive:"+fid)):
                        if old["scope"]=="drive:"+fid:store.revoke(old["source_id"])
                    _discard_drive_document_cache(client,fid)
                    state["access_withdrawn"]+=1
                    state["content_gaps"][fid]={"file_id":fid,"reason":exc.code,"stage":"final_metadata"}
                    state["pending"].pop(0);state["seen_ids"].append(fid);state["files"]+=1;processed+=1
                    store.checkpoint(scope,state)
                    continue
                if exc.code.startswith("document_resume_"):
                    # A busy, invalid or unwritable progress cache cannot mark
                    # the pending file processed. Preserve its place for retry.
                    raise
                if (not exc.code.startswith("document_") and exc.code not in {
                        "source_access_denied", "source_not_found", "source_download_restricted",
                        "gog_export_unsupported"}):
                    raise
                if exc.code=="document_format_unsupported": state["unsupported"]+=1
                # File metadata was readable. An unavailable document/export
                # stage says nothing about independent comment access and must
                # not revoke their evidence or poison the following-file queue.
                # Prior body verification remains unchanged; full inventory
                # cycles revisit the file and clear this gap after a good read.
                state["content_gaps"][fid]={"file_id":fid,"mime_type":meta.get("mimeType", ""),
                    "reason":exc.code,"stage":"document","retry_policy":"next_full_inventory_cycle"}
                if getattr(exc,"operation",None):
                    state["content_gaps"][fid]["operation"]=exc.operation
            progress=SyncProgress()
            comment_access_failed=False
            try:
                for comment in client.iter_drive_comments(fid,progress=progress):
                    for source in normalize_drive_comment(meta,comment,config.get("drive_account_is_professor",False)):
                        source["metadata"]["sync_scope"]=scope
                        changed+=store.upsert(source,verified_remote=True);seen.add(source["source_id"])
                if not progress.complete: raise ConnectorError("drive_comments_partial")
            except ConnectorError as exc:
                if (exc.code in {"gog_auth_required","gog_rate_limited","gog_quota_exceeded"}
                        or (meta.get("mimeType") in READABLE_DRIVE_MIME_TYPES
                            and exc.code not in {"source_access_denied","source_not_found"})):
                    raise
                gap=state["content_gaps"].setdefault(fid,{"file_id":fid,"mime_type":meta.get("mimeType", ""),
                    "reason":exc.code,"stage":"comments","retry_policy":"next_full_inventory_cycle"})
                gap["comment_read_error"]=exc.code
                comment_access_failed=exc.code in {"source_access_denied","source_not_found"}
            # Missing current comments withdraw former evidence, without claiming deletion.
            for old in list(store.sources(platform="drive", scope="drive:"+fid)):
                if old["scope"]=="drive:"+fid and old["source_id"] not in seen:
                    # An unsupported/failed body read cannot withdraw a prior
                    # document. Its previous verification timestamp stays intact.
                    if (document_read and old.get("metadata",{}).get("source_kind")=="document") or ((progress.complete or comment_access_failed) and old.get("metadata",{}).get("source_kind") in {"comment","reply"}):
                        store.revoke(old["source_id"])
        state["pending"].pop(0);state["seen_ids"].append(fid);state["files"]+=1;processed+=1
        store.checkpoint(scope,state)
    complete=state["page_loaded"] and not state["pending"] and not state["page_token"]
    if complete:
        seen_files=set(state["seen_ids"])
        for old in list(store.sources(platform="drive")):
            if old.get("metadata",{}).get("sync_scope")==scope and old.get("metadata",{}).get("file_id") not in seen_files:
                store.revoke(old["source_id"])
        state.update(in_progress=False,last_completed_at=now())
        store.checkpoint(scope,state)
    result={"platform":"drive","state":("complete_for_query_with_content_gaps" if state["content_gaps"] else "complete_for_query") if complete else "partial",
            "sync_scope":scope,"scope_status":"active","inventory_query":query,
            "inventory_complete":complete,"document_read_gaps":list(state["content_gaps"].values()),
            "processed_files":processed,"changed_sources":changed,"cycle_files":state["files"],
            "pages":state["pages"],"pending_files":len(state["pending"]),"more_pages":bool(state["page_token"]),
            "unsupported_document_formats":state["unsupported"],
            "access_withdrawn":state["access_withdrawn"],
            "supported_evidence_refresh":evidence_refresh,
            "supported_mime_types":sorted(READABLE_DRIVE_MIME_TYPES),
            "limitations":["document body authorship remains unverified; requesting-account comments can be direct evidence",
                           "full query inventory is not a claim of complete semantic, image, formula, or historical coverage",
                           "current comments reconciliation, not exhaustive historical/deleted comments",
                           "Drive change feed not wired; repeated full query cycles",*state["extraction_limitations"]]}
    store.coverage(scope,result)
    store.coverage("drive:last_attempt",{**result,"attempt_succeeded":not has_operation_errors(result)})
    return result


def sync_teams_archive(store,config):
    path=Path(config["teams_archive"]).resolve()
    db=sqlite3.connect("file:"+str(path)+"?mode=ro",uri=True);db.row_factory=sqlite3.Row
    teams=set(config["teams_team_ids"]);names=set(config.get("teams_author_names",[]))
    # Include entire conversations involving attributed professor messages, not all
    # third-party channels. Archive identity is never upgraded by display-name match.
    rows=[dict(r) for r in db.execute("SELECT * FROM messages") if r["team_id"] in teams]
    roots={(r["source_id"],r["thread_id"] or r["id"]) for r in rows if r["sender_name"] in names}
    changed=count=0; dates=[]
    for r in rows:
        root=r["thread_id"] or r["id"]
        if (r["source_id"],root) not in roots: continue
        s={"source_id":source_key("teams_archive",r["team_id"],r["source_id"],r["thread_id"],r["id"]),
           "platform":"teams_archive","native_id":r["id"],"author_id":"","author_name":r["sender_name"],
           "authorship":"unverified","status":"active","title":r["subject"],"body":html.unescape(r["body_plain"]),
           "authored_text":"","created_at":r["created_at"],"modified_at":"","url":"",
           "scope":"teams_archive:"+r["team_id"],
           "metadata":{"archive_path":str(path),"archive_updated_at":datetime.fromtimestamp(path.stat().st_mtime,timezone.utc).isoformat(),
                       "container_id":r["source_id"],"parent_id":r["thread_id"],
                       "authorship_basis":"legacy_display_name_only","professor_name_match":r["sender_name"] in names,
                       "live_access_verified":False,"retention_basis":"user_requested_private_research_context"}}
        changed+=store.upsert(s);count+=1;dates.append(r["created_at"])
    db.close()
    result={"platform":"teams_archive","state":"historical_context_only","sources":count,"changed_sources":changed,
            "min_created_at":min(dates) if dates else None,"max_created_at":max(dates) if dates else None,
            "limitations":["legacy archive has no verified Graph author IDs or current access/deletion state",
                           "display-name matches are context only; no active principles may be extracted"]}
    store.coverage("teams_archive",result);return result


def _sync_teams_channels(store,config,client,ids,sync_scope,limit):
    state=store.checkpoint(sync_scope) or {}
    if not state.get("in_progress"):
        pending=[]
        for team in config["teams_team_ids"]:
            pending.extend([[team,c["id"]] for c in client.iter_collection("/teams/"+team+"/channels")])
        state={"in_progress":True,"pending":pending,"started":now(),"channels":0,
               "inventoried_scopes":["teams:"+c for _,c in pending]}
        store.checkpoint(sync_scope,state)
    changed=0;channels=0
    while state["pending"] and channels < limit:
        team,cid=state["pending"][0];scope="teams:"+cid;seen=set()
        for raw in client.iter_channel_messages(team,cid):
            s=normalize_teams(raw,config["teams_tenant_id"],cid,ids)
            s["metadata"]["sync_scope"]=sync_scope
            seen.add(s["source_id"]);changed+=store.upsert(s,verified_remote=True)
        for old in list(store.sources(platform="teams")):
            if old["scope"]==scope and old["source_id"] not in seen: store.revoke(old["source_id"])
        channels+=1;state["channels"]+=1;state["pending"].pop(0)
        store.checkpoint(sync_scope,state)
    complete=not state["pending"]
    if complete:
        for old in list(store.sources(platform="teams")):
            if old.get("metadata",{}).get("sync_scope")==sync_scope and old["scope"] not in state["inventoried_scopes"]:
                store.revoke(old["source_id"])
        state["in_progress"]=False;store.checkpoint(sync_scope,state)
    result={"platform":"teams","state":"complete_configured_channels" if complete else "partial",
            "channels":channels,"cycle_channels":state["channels"],"pending_channels":len(state["pending"]),"changed_sources":changed,
            "limitations":["configured team channels only; private chats are accounted separately",
                           "Graph token env supplied externally; app reauthentication not managed by this service"]}
    store.coverage("teams:channels",result);return result


def _sync_teams_chats(store,config,client,ids,sync_scope,limit):
    """Checkpoint actual pages and reconcile only fully enumerated chat scopes."""
    route_scope=sync_scope+":chats"
    page_limit=config.get("teams_chat_page_limit",10)
    if type(page_limit) is not int or page_limit < 1:
        raise ConnectorError("teams_chat_page_limit_invalid")
    state=store.checkpoint(route_scope) or {}
    if not state.get("in_progress"):
        state={"in_progress":True,"started":now(),"pending_users":sorted(ids),"user_cursors":{},
               "pending":[],"inventoried_chat_ids":[],"message_states":{},"chats":0,"pages":0,"inventory_pages":0}
        store.checkpoint(route_scope,state)
    errors=[];changed=messages=completed=attempted=0
    # Each identity owns a different collection path. A failed identity cannot
    # establish absence in another one, and cannot block already observed chats.
    for uid in list(state["pending_users"]):
        progress=SyncProgress(remaining_page_token=state["user_cursors"].get(uid))
        try:
            for chat in client.list_user_chats(uid,progress=progress,max_pages=page_limit,start_page=state["user_cursors"].get(uid)):
                cid=chat.get("id")
                if not isinstance(cid,str) or not cid or cid.startswith("-") or "\x00" in cid:
                    raise ConnectorError("teams_chat_inventory_invalid")
                if cid not in state["inventoried_chat_ids"]:
                    state["inventoried_chat_ids"].append(cid);state["pending"].append(cid)
                state["user_cursors"][uid]=progress.remaining_page_token
                store.checkpoint(route_scope,state)
        except ConnectorError as exc:
            errors.append({"stage":"chat_inventory","error_code":exc.code})
        finally:
            state["inventory_pages"]+=progress.pages
            state["user_cursors"][uid]=progress.remaining_page_token
            if progress.complete:
                state["pending_users"].remove(uid);state["user_cursors"].pop(uid,None)
            store.checkpoint(route_scope,state)
    # Limit counts attempted containers; page_limit bounds each one's work. A
    # failing/large first chat does not prevent other pending chats this cycle.
    for cid in list(state["pending"])[:limit]:
        attempted+=1
        message_state=state["message_states"].setdefault(cid,{"cursor":None,"seen":[]})
        seen=set(message_state["seen"]);progress=SyncProgress(remaining_page_token=message_state["cursor"])
        try:
            for raw in client.iter_chat_messages(cid,progress=progress,max_pages=page_limit,start_page=message_state["cursor"]):
                if raw.get("chatId") and raw["chatId"]!=cid:
                    raise ConnectorError("teams_chat_message_scope_changed")
                source=normalize_teams(raw,config["teams_tenant_id"],cid,ids)
                source["metadata"].update(sync_scope=route_scope,container_type="chat",chat_id=cid)
                changed+=store.upsert(source,verified_remote=True);messages+=1;seen.add(source["source_id"])
                message_state.update(cursor=progress.remaining_page_token,seen=sorted(seen))
                store.checkpoint(route_scope,state)
        except ConnectorError as exc:
            errors.append({"stage":"chat_messages","error_code":exc.code})
        finally:
            state["pages"]+=progress.pages
            message_state.update(cursor=progress.remaining_page_token,seen=sorted(seen))
            store.checkpoint(route_scope,state)
        if progress.complete:
            # A fully read chat is the only message-absence inventory. Failure
            # or a page cap leaves every unseen old message intact.
            for old in list(store.sources(platform="teams")):
                metadata=old.get("metadata",{})
                exact_chat=(metadata.get("tenant_id")==config["teams_tenant_id"] and metadata.get("container_id")==cid
                            and metadata.get("container_type")=="chat" and old["scope"]=="teams:"+cid)
                # Exact native-snapshot records share the same source-service
                # identity and must also be withdrawn after a complete live read.
                # Name-only, other-tenant and unknown-container records are not
                # an absence inference we can establish with this enumeration.
                if exact_chat and old["source_id"] not in seen:
                    store.revoke(old["source_id"])
            state["pending"].remove(cid);state["message_states"].pop(cid,None)
            state["chats"]+=1;completed+=1
            store.checkpoint(route_scope,state)
        else:
            # Rotation also prevents starvation when limit=1 and the first chat
            # repeatedly fails or needs more pages than one invocation permits.
            state["pending"].remove(cid);state["pending"].append(cid)
            store.checkpoint(route_scope,state)
    inventory_complete=not state["pending_users"]
    complete=inventory_complete and not state["pending"]
    if complete:
        current=set(state["inventoried_chat_ids"])
        for old in list(store.sources(platform="teams")):
            meta=old.get("metadata",{})
            if meta.get("sync_scope")==route_scope and meta.get("chat_id") not in current:
                store.revoke(old["source_id"])
        state.update(in_progress=False,last_completed_at=now());store.checkpoint(route_scope,state)
    outcome="complete_configured_chats" if complete else "error" if errors and not messages and not completed else "partial"
    result={"platform":"teams","state":outcome,"inventory_complete":inventory_complete,
            "chats":completed,"attempted_chats":attempted,"cycle_chats":state["chats"],
            "pending_chats":len(state["pending"]),"pending_chat_inventories":len(state["pending_users"]),
            "message_pages":state["pages"],"inventory_pages":state["inventory_pages"],
            "processed_messages":messages,"changed_sources":changed,"errors":errors,
            "limitations":["verified user chat membership only; historical removed memberships are not discoverable",
                           "attachments are metadata context only; chat text identity uses verified Graph author IDs",
                           "partial chat lists and message pages never establish deletion"]}
    if outcome=="error":result["error_code"]=errors[0]["error_code"]
    store.coverage("teams:chats",result);return result


def sync_teams(store,config,limit):
    if type(limit) is not int or limit < 1:raise ValueError("positive per-route container limit required")
    client=TeamsGraphClient.from_config(config,store.home)
    ids=set(config.get("teams_professor_ids",[]))
    if not ids: raise ConnectorError("verified_teams_professor_ids_required")
    sync_scope="teams:"+digest({"teams":config["teams_team_ids"],"professor_ids":sorted(ids),"tenant":config["teams_tenant_id"]})[:16]
    include_chats=config.get("teams_include_chats") is True
    def run_route(name,operation):
        try:return operation()
        except Exception as exc:
            result={"platform":"teams","state":"error","error_type":type(exc).__name__,
                    "error_code":exc.code if isinstance(exc,ConnectorError) else "sync_failed","checkpoint_preserved":True}
            store.coverage("teams:"+name,result);return result
    channels=run_route("channels",lambda:_sync_teams_channels(store,config,client,ids,sync_scope,limit))
    chats=run_route("chats",lambda:_sync_teams_chats(store,config,client,ids,sync_scope,limit)) if include_chats else {"state":"disabled"}
    if not include_chats:
        result={**channels,"channel_sync":channels,"chat_sync":chats}
    else:
        complete=channels["state"]=="complete_configured_channels" and chats["state"]=="complete_configured_chats"
        both_failed=channels["state"]==chats["state"]=="error"
        result={"platform":"teams","state":"complete_configured_channels_and_chats" if complete else "error" if both_failed else "partial",
                "channel_sync":channels,"chat_sync":chats,"channels":channels.get("channels",0),"chats":chats.get("chats",0),
                "cycle_channels":channels.get("cycle_channels",0),"cycle_chats":chats.get("cycle_chats",0),
                "pending_channels":channels.get("pending_channels",0),"pending_chats":chats.get("pending_chats",0),
                "changed_sources":channels.get("changed_sources",0)+chats.get("changed_sources",0),
                "limitations":["channel and chat routes have independent checkpoints, permissions and completeness",
                               "bound device-code mode refreshes silently; legacy environment mode requires an external token",
                               "authentication success does not prove complete channel/chat history"]}
        if both_failed:result["error_code"]="teams_routes_failed"
    store.coverage("teams",result);return result


def has_operation_errors(result):
    """A bounded partial batch is progress; an unsuccessful child route is an error."""
    if not isinstance(result,dict):return False
    if result.get("state") in {"error","failed"} or result.get("has_errors") is True:return True
    if result.get("errors") or result.get("failures") or result.get("failure_codes"):return True
    if type(result.get("failed_sources")) is int and result["failed_sources"]>0:return True
    return any(has_operation_errors(result.get(key)) for key in
               ("channel_sync","chat_sync","supported_evidence_refresh"))


@contextmanager
def _sync_lock(home, name, mode):
    descriptor = os.open(home / name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a") as lock:
        os.fchmod(lock.fileno(), 0o600)
        try:
            fcntl.flock(lock, mode | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True


def sync(store,config,platform="all",limit=100):
    if limit < 1: raise ValueError("positive per-run limit required")
    operations={"gmail":lambda:sync_gmail(store,config,GogClient(config["account"]),limit),
                "drive":lambda:sync_drive(store,config,GogClient(config["account"],document_cache_home=store.home,
                    **{name:config[key] for name,key in (
                        ("slides_text_executable","drive_slides_text_executable"),
                        ("docs_text_executable","drive_docs_text_executable"),
                        ("pdf_ocr_tessdata","drive_pdf_ocr_tessdata")) if key in config}),limit),
                "teams":lambda:sync_teams(store,config,limit),
                "teams_archive":lambda:sync_teams_archive(store,config)}
    if platform != "all" and platform not in operations:
        raise ValueError("invalid sync platform")
    names=list(operations) if platform=="all" else [platform]
    results=[]
    # Maintenance still takes sync.lock exclusively. Source readers share it
    # while a separate exclusive lock prevents overlapping the same platform.
    with _sync_lock(store.home, "sync.lock", fcntl.LOCK_SH) as coordinated:
        if not coordinated:
            return {"state":"already_running"}
        for name in names:
            with _sync_lock(store.home, "sync." + name + ".lock", fcntl.LOCK_EX) as acquired:
                if not acquired:
                    if len(names) == 1:
                        return {"state":"already_running"}
                    results.append({"platform":name,"state":"already_running","checkpoint_preserved":True})
                    continue
                try:
                    result=operations[name]()
                    store.coverage(name+":last_attempt",{**result,"attempt_succeeded":not has_operation_errors(result)})
                    results.append(result)
                except Exception as exc:
                    result={"platform":name,"state":"error","error_type":type(exc).__name__,
                            "error_code":exc.code if isinstance(exc,ConnectorError) else "sync_failed",
                            "checkpoint_preserved":True}
                    store.coverage(name+":last_attempt",result);results.append(result)
    return {"results":results,"overall_complete":False,
            "note":"Per-scope coverage, historical membership and unsupported sources require separate accounting."}
