"""One bounded refresh cycle; local status only, no promotion or service startup.

The outer lock is distinct from sync.lock and distill.lock. Source reads inherit
connector request timeouts/retries. Configured Teams authentication may renew
tokens silently; this module does not start a daemon, prompt for credentials,
send notifications, or claim historical scope completeness.
"""
from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import tempfile
from threading import Barrier, Lock
import urllib.request

from .distill import DEFAULT_ENDPOINT, DEFAULT_MODEL, extractor_fingerprint
from .runner import distill_pending
from .store import Store, digest
from .sync import sync
from .teams_auth import SAFE_ERROR_CODES as TEAMS_AUTH_ERRORS
from .document_resume import SAFE_ERROR_CODES as DOCUMENT_RESUME_ERRORS
from .pdf_ocr import SAFE_ERROR_CODES as PDF_OCR_ERRORS

TEAMS_LIMIT = 3
_SUCCESS_STATES = {"partial", "complete_for_query", "complete_for_query_with_content_gaps",
                   "complete_configured_channels", "complete_configured_channels_and_chats", "historical_context_only"}
_COMPLETE_STATES = {"complete_for_query", "complete_for_query_with_content_gaps",
                    "complete_configured_channels", "complete_configured_channels_and_chats"}
_SAFE_ERRORS = {
    "gog_native_export_timeout", "gog_native_export_size_limit", "gog_export_unsupported",
    "gog_rate_limited", "gog_quota_exceeded", "source_download_restricted",
    "document_partial_text_extraction", "document_native_doc_timeout",
    "document_slide_response_invalid", "document_slide_inventory_changed", "document_changed_during_read",
    "document_slide_revision_required", "document_slide_node_budget_exceeded",
    "document_slide_group_depth_exceeded", "document_slide_count_budget_exceeded",
    "gog_slides_text_executable_invalid",
    "gog_docs_text_executable_invalid", "document_docs_revision_required",
    "document_docs_response_invalid", "document_docs_tab_inventory_invalid",
    "document_docs_tab_depth_exceeded", "document_docs_tab_budget_exceeded",
    "document_docs_content_depth_exceeded", "document_docs_node_budget_exceeded",
    "document_docs_preview_access_denied", "document_docs_inline_access_denied",
    "document_docs_verification_access_denied",
    "teams_routes_failed",
    "text_body_requires_attachment_fetch", "gmail_body_size_missing", "gmail_body_budget_exceeded",
    "gmail_body_download_mismatch", "undecodable_message_charset", "message_full_body_missing",
    "malformed_mime_part", "malformed_mime_children",
    "sync_failed", "gog_auth_required", "gog_read_failed", "gog_execution_failed", "gog_connect_timeout",
    "gog_invalid_json", "gog_pagination_envelope_missing", "gmail_search_shape", "drive_inventory_shape", "drive_incomplete_search",
    "drive_comments_partial", "drive_pagination_cycle", "drive_page_token_invalid",
    "drive_refresh_account_unverified", "drive_refresh_metadata_identity_mismatch",
    "drive_refresh_duplicate_source", "drive_refresh_registry_changed", "drive_supported_refresh_failed",
    "source_access_denied", "source_not_found", "teams_auth_required", "teams_access_denied",
    "teams_source_unavailable", "teams_full_resync_required", "graph_throttled", "graph_read_failed",
    "graph_network_failed", "graph_invalid_json", "graph_invalid_response", "graph_retry_later",
    "graph_nextlink_scope_changed", "graph_collection_missing", "graph_replies_invalid",
    "graph_reply_nextlink_scope_changed", "pagination_cycle", "unexpected_collection_shape", "invalid_page_token",
    "teams_token_missing_or_invalid", "verified_teams_professor_ids_required",
    "backend_transport", "backend_error", "backend_not_local", "backend_redirect",
    "response_truncated", "response_empty", "response_json", "response_incomplete",
    "schema_type", "schema_identity", "schema_enum", "schema_fields", "schema_count",
    "schema_duplicate", "schema_length", "outcome_mismatch", "abstention_reason",
    "quote_mismatch", "control_directive", "source_empty", "source_invalid",
    "source_ineligible", "source_hash_mismatch", "store_commit_failed", "policy_mismatch",
    "document_format_unsupported", "document_xml_parser_unavailable", "document_text_budget_exceeded",
    "document_archive_budget_exceeded", "document_archive_duplicate_parts", "document_office_parse_failed",
    "document_slide_relationship_missing", "document_slide_relationship_invalid",
    "document_notes_relationship_invalid", "document_conversion_failed", "document_pdf_parse_failed",
    "document_all_tabs_not_verified", "document_slide_inventory_invalid", "document_size_metadata_required",
    "document_download_budget_exceeded", "document_download_invalid", "document_slide_count_changed",
    "document_pdf_parser_unavailable", "document_pdf_requires_ocr", "document_text_encoding_unsupported",
    "document_all_sheets_not_verified", "document_sheet_title_invalid", "document_sheet_grid_unavailable",
    "document_sheet_cell_budget_exceeded", "document_sheet_response_invalid", "document_response_budget_exceeded",
}
_COUNT_FIELDS = {"processed_threads", "processed_files", "changed_sources", "cycle_threads", "cycle_files",
                 "pages", "pending_threads", "pending_files", "unsupported_document_formats",
                 "access_withdrawn", "channels", "cycle_channels", "pending_channels", "sources"}
_COUNT_FIELDS |= {"chats", "cycle_chats", "pending_chats", "processed_chats",
                  "due_threads", "refreshed_threads", "due_files", "refreshed_files",
                  "sources_not_due", "withdrawn_sources"}
_TIME_KEYS = ("gmail", "drive", "teams", "teams_archive", "inference")
_SAFE_ERRORS.update(TEAMS_AUTH_ERRORS)
_SAFE_ERRORS.update(DOCUMENT_RESUME_ERRORS)
_SAFE_ERRORS.update("document_" + code for code in PDF_OCR_ERRORS)
_SAFE_ERRORS.add("document_pdf_ocr_revision_required")
_SAFE_ERRORS.update({"teams_token_provider_invalid","teams_auth_provider_failed","teams_reauth_required"})
_SAFE_ERRORS.add("gmail_lab_project_routes_invalid")
_SAFE_ERRORS.add("source_store_unavailable")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _safe_error(value, default="source_sync_failed"):
    return value if isinstance(value, str) and value in _SAFE_ERRORS else default


def _count(value):
    return value if type(value) is int and value >= 0 else 0


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return value if parsed.tzinfo else None
    except ValueError:
        return None


def _load_config(store, config):
    if isinstance(config, dict):
        return dict(config)
    path = Path(config) if config is not None else store.home / "config.json"
    with path.open("rb") as file:
        content = file.read(1_048_577)
    if len(content) > 1_048_576:
        raise ValueError("configuration exceeds size limit")
    result = json.loads(content)
    if not isinstance(result, dict):
        raise ValueError("configuration must be an object")
    return result


def _previous_times(path):
    empty = {key: None for key in _TIME_KEYS}
    try:
        with path.open("rb") as file:
            raw = file.read(262_145)
        previous = json.loads(raw) if len(raw) <= 262_144 else {}
        success = previous.get("last_success_times", {})
        complete = previous.get("last_scope_complete_times", {})
        if not isinstance(success, dict) or not isinstance(complete, dict):
            return empty.copy(), empty.copy()
        return ({key: _timestamp(success.get(key)) for key in _TIME_KEYS},
                {key: _timestamp(complete.get(key)) for key in _TIME_KEYS})
    except (OSError, ValueError, TypeError, AttributeError):
        return empty.copy(), empty.copy()


def _write_status(path, value):
    """Readers see either the old complete JSON or the new complete JSON."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".refresh-status-", suffix=".tmp", delete=False) as file:
            temporary = Path(file.name)
            os.fchmod(file.fileno(), 0o600)
            json.dump(value, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def local_ollama_available():
    """Probe only local server metadata. Never load a model or start a service."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(DEFAULT_ENDPOINT + "/api/version", timeout=3) as response:
            raw = response.read(1025)
        value = json.loads(raw) if len(raw) <= 1024 else None
        return (isinstance(value, dict) and isinstance(value.get("version"), str)
                and 0 < len(value["version"]) <= 64)
    except (OSError, ValueError, TypeError):
        return False


def _source_summary(platform, result):
    if not isinstance(result, dict):
        return {"platform": platform, "state": "error", "error_code": "sync_result_invalid"}
    if result.get("state") == "already_running":
        return {"platform": platform, "state": "already_running", "attempt_succeeded": False}
    rows = result.get("results")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        return {"platform": platform, "state": "error", "error_code": "sync_result_invalid"}
    row = rows[0]
    if row.get("platform") != platform or row.get("state") not in _SUCCESS_STATES | {"error"}:
        return {"platform": platform, "state": "error", "error_code": "sync_result_invalid"}
    from .sync import has_operation_errors
    summary = {"platform": platform, "state": row["state"],
               "has_errors":has_operation_errors(row),
               "attempt_succeeded": not has_operation_errors(row)}
    for key in _COUNT_FIELDS:
        if key in row:
            summary[key] = _count(row[key])
    for key in ("more_pages", "inventory_complete"):
        if type(row.get(key)) is bool:
            summary[key] = row[key]
    if row["state"] == "error":
        summary["error_code"] = _safe_error(row.get("error_code"))
    for route in ("channel_sync","chat_sync","supported_evidence_refresh"):
        value=row.get(route)
        if isinstance(value,dict):
            # No raw message IDs, nextLinks or provider text in recurring status.
            state=value.get("state")
            allowed=_SUCCESS_STATES|{"error","disabled","not_configured","partial_inventory",
                                    "complete_configured_chats","complete_due_supported_evidence"}
            summary[route]={"state":state if state in allowed else "unknown"}
            for key in _COUNT_FIELDS:
                if key in value:summary[route][key]=_count(value[key])
            if value.get("error_code"):summary[route]["error_code"]=_safe_error(value["error_code"])
            failures=value.get("errors",value.get("failures"))
            if isinstance(failures,list):
                summary[route]["failure_codes"]=dict(Counter(
                    _safe_error(e.get("error_code") if isinstance(e,dict) else None)
                    for e in failures))
    gaps = row.get("document_read_gaps")
    if isinstance(gaps, list):
        summary["content_gap_count"] = len(gaps)
        summary["content_gap_codes"] = dict(Counter(
            _safe_error(gap.get("reason") if isinstance(gap, dict) else None, "document_read_failed")
            for gap in gaps))
    return summary


def _sync_one(store, config, platform, limit):
    try:
        summary = _source_summary(platform, sync(store, config, platform=platform, limit=limit))
    except Exception:
        # Connector exceptions can contain private provider messages. No details
        # from an arbitrary exception are copied to status or coverage records.
        summary = {"platform": platform, "state": "error", "error_code": "source_sync_failed"}
    summary["finished_at"] = _now()
    store.coverage(platform + ":refresh_attempt", summary)
    return summary


def _sync_worker(home, config, platform, limit, initialization_lock, initialized):
    """Own the SQLite connection in this worker, including its close."""
    worker = None
    try:
        try:
            # Store startup performs schema/PRAGMA checks. Finish all startup
            # checks before any worker begins writing source data.
            with initialization_lock:
                worker = Store(home)
        except Exception:
            pass
        finally:
            initialized.wait()
        if worker is None:
            return {"platform": platform, "state": "error", "error_code": "source_store_unavailable",
                    "finished_at": _now()}
        return _sync_one(worker, config, platform, limit)
    finally:
        if worker is not None:
            worker.close()


def _parallel_sources(store, config, jobs, cycle_lock_fd):
    initialization_lock, initialized = Lock(), Barrier(len(jobs))
    pool = ThreadPoolExecutor(max_workers=min(3, len(jobs)), thread_name_prefix="cha-source")
    try:
        futures = []
        for platform, limit in jobs:
            # A caller may catch KeyboardInterrupt and stay alive. Retain the
            # outer flock until running workers finish even after propagation.
            guard = os.dup(cycle_lock_fd)
            try:
                future = pool.submit(_sync_worker, store.home, config, platform, limit,
                                     initialization_lock, initialized)
            except BaseException:
                os.close(guard)
                raise
            future.add_done_callback(lambda _future, descriptor=guard: os.close(descriptor))
            futures.append((platform, future))
        results = {}
        # Workers persist coverage themselves as soon as they finish. Waiting
        # in platform order only makes the returned summary deterministic.
        for platform, future in futures:
            try:
                summary = future.result()
            except Exception:
                summary = {"platform": platform, "state": "error", "error_code": "source_sync_failed",
                           "finished_at": _now()}
                store.coverage(platform + ":refresh_attempt", summary)
            if summary.get("error_code") == "source_store_unavailable":
                store.coverage(platform + ":refresh_attempt", summary)
            results[platform] = summary
    except BaseException:
        # Propagate interrupts promptly to the caller. Running threads/network
        # calls are not cancelled; a CLI must handle process exit separately.
        initialized.abort()
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        pool.shutdown(wait=True)
        return results


def _archive_signature(config):
    path = Path(config["teams_archive"]).expanduser().resolve()
    stat = path.stat()
    if not path.is_file():
        raise ValueError("archive must be a file")
    wal = path.with_name(path.name + "-wal")
    wal_stat = wal.stat() if wal.exists() else None
    return digest({"path": str(path), "mtime_ns": stat.st_mtime_ns, "size": stat.st_size,
                   "wal_mtime_ns": wal_stat.st_mtime_ns if wal_stat else None,
                   "wal_size": wal_stat.st_size if wal_stat else None,
                   "teams": sorted(config.get("teams_team_ids", [])),
                   "authors": sorted(config.get("teams_author_names", []))})


def _archive_refresh(store, config):
    platform = "teams_archive"
    if not config.get("teams_archive"):
        summary = {"platform": platform, "state": "not_configured", "historical_context_only": True}
    else:
        try:
            signature = _archive_signature(config)
            previous = store.checkpoint("refresh:teams_archive") or {}
            if previous.get("archive_signature") == signature:
                summary = {"platform": platform, "state": "unchanged", "processed_sources": 0,
                           "historical_context_only": True,
                           "last_imported_at": _timestamp(previous.get("last_imported_at"))}
            else:
                summary = _sync_one(store, config, platform, limit=1)
                if summary["state"] == "historical_context_only":
                    if _archive_signature(config) == signature:
                        store.checkpoint("refresh:teams_archive", {
                            "archive_signature": signature, "last_imported_at": _now()})
                    else:
                        summary.update(state="changed_during_import", attempt_succeeded=False)
                summary["historical_context_only"] = True
        except Exception:
            summary = {"platform": platform, "state": "error", "error_code": "archive_read_failed",
                       "historical_context_only": True}
    store.coverage(platform + ":refresh_attempt", summary)
    return summary


def _inference_refresh(store, limit, model):
    fingerprint = extractor_fingerprint(model)
    pending = sum(1 for _ in store.pending_extraction(extractor_id=fingerprint))
    common = {"pending": pending, "extractor_id": fingerprint, "automatic_activation": False}
    if limit == 0:
        return {**common, "state": "disabled", "attempted_sources": 0}
    if pending == 0:
        return {**common, "state": "no_pending_sources", "attempted_sources": 0}
    if not local_ollama_available():
        return {**common, "state": "backend_unavailable", "attempted_sources": 0,
                "retry_state_unchanged": True}
    try:
        result = distill_pending(store, limit=limit, model=model)
        if result.get("state") == "already_running":
            return {**common, "state": "already_running", "attempted_sources": 0}
        rows = result.get("results", [])
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("invalid inference summary")
        complete = sum(row.get("state") == "complete" for row in rows)
        failed = sum(row.get("state") == "failed" for row in rows)
        summary = {**common, "state": "partial" if failed and complete else "error" if failed else
                   "complete_bounded_batch" if complete else "no_ready_sources",
                   "attempted_sources": len(rows), "completed_sources": complete, "failed_sources": failed,
                   "candidates_returned": sum(_count(row.get("candidates")) for row in rows),
                   "inference_calls": sum(_count(row.get("inference_calls")) for row in rows),
                   "failure_codes": dict(Counter(_safe_error(row.get("error_code"), "extraction_failed")
                                                 for row in rows if row.get("state") == "failed"))}
        for key in ("pending", "ready", "deferred", "held"):
            if key in result:
                summary[key] = _count(result[key])
        return summary
    except Exception:
        return {**common, "state": "error", "error_code": "inference_cycle_failed"}


def refresh(store, config=None, gmail_limit=100, drive_limit=10, distill_limit=3, model=DEFAULT_MODEL):
    """Sync once and optionally extract candidates, leaving a private status file."""
    for name, value, minimum in (("gmail_limit", gmail_limit, 1), ("drive_limit", drive_limit, 1),
                                 ("distill_limit", distill_limit, 0)):
        if type(value) is not int or value < minimum:
            raise ValueError(name + " must be a valid integer budget")
    status_path = store.home / "refresh_status.json"
    descriptor = os.open(store.home / "refresh.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a") as lock:
        os.fchmod(lock.fileno(), 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"state": "already_running", "overall_complete": False, "automatic_activation": False}
        started = _now()
        success_times, complete_times = _previous_times(status_path)
        try:
            configuration = _load_config(store, config)
        except Exception:
            configuration = None
        source_results = {}
        if configuration is None:
            source_results["config"] = {"state": "error", "error_code": "config_load_failed"}
        else:
            jobs = [("gmail", gmail_limit), ("drive", drive_limit)]
            teams_configured = "teams_auth" in configuration or bool(os.environ.get("CHA_TEAMS_ACCESS_TOKEN", "").strip())
            if teams_configured:
                jobs.append(("teams", TEAMS_LIMIT))
            else:
                teams_status = {"platform": "teams", "state": "not_configured",
                                "error_code": "teams_token_missing_or_invalid", "route": "graph_channels"}
                store.coverage("teams:refresh_attempt", teams_status)
            source_results = _parallel_sources(store, configuration, jobs, lock.fileno())
            if not teams_configured:
                source_results["teams"] = teams_status
            source_results["teams_archive"] = _archive_refresh(store, configuration)
        for platform, result in source_results.items():
            if result.get("state") in _SUCCESS_STATES and not result.get("has_errors"):
                success_times[platform] = _timestamp(result.get("finished_at")) or _now()
            elif platform == "teams_archive" and result.get("state") == "unchanged":
                success_times[platform] = _timestamp(result.get("last_imported_at")) or success_times[platform]
            if result.get("state") in _COMPLETE_STATES:
                complete_times[platform] = success_times[platform]
        inference = _inference_refresh(store, distill_limit, model)
        if inference.get("completed_sources", 0):
            success_times["inference"] = _now()
        candidates = store.db.execute("SELECT COUNT(*) FROM principles WHERE status='candidate'").fetchone()[0]
        scope_gaps = ["historical_lab_membership_not_exhaustive",
                      "drive_document_authorship_unverified", "philosophy_transfer_quality_not_fully_validated"]
        if source_results.get("teams",{}).get("state")!="complete_configured_channels_and_chats":
            scope_gaps.append("teams_private_chat_inventory_not_exhaustive")
        for platform, result in source_results.items():
            if result.get("state") in {"partial", "error", "not_configured", "already_running", "changed_during_import"}:
                scope_gaps.append(platform + "_refresh_" + result["state"])
            if result.get("content_gap_count", 0):
                scope_gaps.append(platform + "_content_gaps")
        if configuration and configuration.get("teams_archive"):
            scope_gaps.append("teams_archive_is_historical_unverified_context")
        if inference["state"] in {"backend_unavailable", "error", "partial", "disabled", "already_running"}:
            scope_gaps.append("inference_" + inference["state"])
        if inference.get("pending", 0):
            scope_gaps.append("extraction_backlog")
        if candidates:
            scope_gaps.append("candidates_require_explicit_review")
        result = {"schema_version": 1, "state": "finished_bounded_cycle", "started_at": started,
                  "health":"source_error" if any(r.get("state")=="error" or r.get("has_errors") for r in source_results.values())
                            else "attention_required" if scope_gaps else "ready",
                  "finished_at": _now(), "overall_complete": False,
                  "budgets": {"gmail_threads": gmail_limit, "drive_files": drive_limit,
                              "teams_channels": TEAMS_LIMIT, "distill_sources": distill_limit},
                  "sources": source_results, "inference": inference, "scope_gaps": sorted(set(scope_gaps)),
                  "candidates_awaiting_review": candidates, "last_success_times": success_times,
                  "last_scope_complete_times": complete_times, "automatic_activation": False,
                  "external_notifications": False}
        _write_status(status_path, result)
        return result
