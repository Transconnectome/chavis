"""Bounded local extraction with revision-specific retries and atomic completion."""
from datetime import datetime, timedelta, timezone
import fcntl

from .distill import ExtractionError, LocalOllamaClient, extract, extractor_fingerprint
from .store import SourceExcludedError

MAX_SOURCE_ATTEMPTS = 3
RETRY_BASE_SECONDS = 300
RETRY_MAX_SECONDS = 86400


def _now():
    return datetime.now(timezone.utc)


def _retry_time(value):
    try:
        result = datetime.fromisoformat(value)
        return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _attempts(record):
    if not record or record["status"] != "failed":
        return 0
    value = record.get("details", {}).get("attempts", 1)
    return value if type(value) is int and value >= 1 else 1


def _pending_queue(store, at, extractor_id):
    ready, deferred, held = [], 0, 0
    for source in store.pending_extraction(extractor_id=extractor_id):
        record = store.extraction_record(source)
        if record and record.get("details", {}).get("extractor_id") != extractor_id:
            # A fixed prompt/schema/model must get a fresh attempt even if the
            # previous policy repeatedly failed or returned no_principles.
            record = None
        details = record.get("details", {}) if record else {}
        failed = bool(record and record["status"] == "failed")
        if failed and (details.get("held") or details.get("retryable") is False
                       or _attempts(record) >= MAX_SOURCE_ATTEMPTS):
            held += 1
            continue
        retry_at = _retry_time(details.get("next_retry_at")) if failed else None
        if retry_at and retry_at > at:
            deferred += 1
            continue
        # Untouched records cannot be starved by deterministic bad output from
        # an earlier source. Due retries are ordered by their last attempt.
        ready.append((failed, record["updated_at"] if record else source.get("fetched_at", ""),
                      source["source_id"], source, record))
    ready.sort(key=lambda row: row[:3])
    return ready, deferred, held


def distill_pending(store, *, limit=10, model="qwen3.6:35b-ctx16k"):
    if type(limit) is not int or limit < 1:
        raise ValueError("positive integer limit required")
    results = []
    extractor_id = extractor_fingerprint(model)
    with (store.home / "distill.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"state": "already_running", "automatic_activation": False}
        queue, _, _ = _pending_queue(store, _now(), extractor_id)
        client = LocalOllamaClient(model=model)
        for _, _, _, source, previous in queue[:limit]:
            result = None
            inference_started = False
            try:
                # The queue is a snapshot; a review can exclude later entries
                # while an earlier source is being processed.
                if store.source_exclusion(source["source_id"]):
                    raise SourceExcludedError()
                inference_started = True
                result = extract(source, client)
                if result.get("extractor_id") != extractor_id:
                    raise ExtractionError("extraction policy changed during the batch",
                                          code="policy_mismatch", retryable=False,
                                          completed_chunks=result["chunks_processed"],
                                          total_chunks=result["chunks_total"],
                                          inference_calls=result["inference_calls"])
                # This Store method validates the current revision and commits
                # every candidate, quote, audit row and completion as one unit.
                ids = store.commit_extraction(source, result)
                results.append({"source_id": source["source_id"], "state": "complete",
                                "candidates": len(ids), "chunks": result["chunks_total"],
                                "inference_calls": result["inference_calls"]})
            except Exception as exc:
                if isinstance(exc, SourceExcludedError) or store.source_exclusion(source["source_id"]):
                    # A completed/failed call cannot be undone, but withdrawn
                    # eligibility must not consume a retry or mark completion.
                    calls = (result.get("inference_calls") if isinstance(result, dict) else
                             exc.inference_calls if isinstance(exc, ExtractionError) else
                             None if inference_started else 0)
                    results.append({"source_id": source["source_id"], "state": "skipped",
                                    "reason": "source_excluded",
                                    "inference_calls": calls if type(calls) is int and calls >= 0 else None})
                    if isinstance(exc, ExtractionError) and exc.backend_failure:
                        break
                    continue
                if isinstance(exc, ExtractionError):
                    details = {"error_type": "ExtractionError", "error_code": exc.code,
                               "field": exc.field, "retryable": exc.retryable,
                               "chunks_processed": exc.completed_chunks,
                               "chunks_total": exc.total_chunks, "failed_chunk": exc.failed_chunk,
                               "inference_calls": exc.inference_calls}
                    backend_failure = exc.backend_failure
                else:
                    # Never serialize arbitrary exception messages: they may
                    # contain a private quote, identifier, or model response.
                    details = {"error_type": type(exc).__name__, "error_code": "store_commit_failed",
                               "retryable": True,
                               "chunks_processed": result["chunks_processed"] if result else 0,
                               "chunks_total": result["chunks_total"] if result else 0,
                               "inference_calls": result["inference_calls"] if result else 0}
                    backend_failure = False
                attempts = _attempts(previous) + 1
                held = not details["retryable"] or attempts >= MAX_SOURCE_ATTEMPTS
                delay = min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * (2 ** (attempts - 1)))
                details.update(attempts=attempts, held=held, extractor_id=extractor_id,
                               next_retry_at=None if held else (_now() + timedelta(seconds=delay)).isoformat())
                store.mark_extraction(source, "failed", **details)
                results.append({"source_id": source["source_id"], "state": "failed", **details})
                # An unavailable local backend affects all sources; do not
                # consume the whole queue or their retries on the same outage.
                if backend_failure:
                    break
        ready, deferred, held = _pending_queue(store, _now(), extractor_id)
    return {"results": results, "pending": len(ready) + deferred + held,
            "ready": len(ready), "deferred": deferred, "held": held,
            "automatic_activation": False, "extractor_id": extractor_id}
