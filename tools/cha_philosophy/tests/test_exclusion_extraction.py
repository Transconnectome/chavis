"""Explicit attribution reviews remain effective across an extraction batch."""
import json

import pytest

from tools.cha_philosophy.distill import ExtractionError, extractor_fingerprint
from tools.cha_philosophy.runner import distill_pending
from tools.cha_philosophy.store import SourceExcludedError, Store
from tools.cha_philosophy.tests.test_store import extraction_result, source


@pytest.fixture
def stores(tmp_path):
    main = Store(tmp_path / "private")
    reviewer = Store(main.home)
    yield main, reviewer
    reviewer.close()
    main.close()


def add(store, suffix="a"):
    src = source(source_id="gmail/test/thread/" + suffix,
                 authored_text="SYNTHETIC_PRIVATE_SOURCE evidence must support a bounded claim.",
                 body="SYNTHETIC_PRIVATE_SOURCE evidence must support a bounded claim.")
    store.upsert(src, verified_remote=True)
    return store.get_source(src["source_id"])


def review(store, src, exclude):
    store.review_source_exclusion(src["source_id"], source_hash=src["source_hash"],
                                  exclude=exclude, reviewer="synthetic-reviewer",
                                  reason="Synthetic independent attribution review.")


def complete(src, nonempty=False):
    principles = [{"statement": "SYNTHETIC_MODEL_TEXT bounded interpretation.",
                   "domains": ["review"], "evidence": [{"source_id": src["source_id"],
                   "source_hash": src["source_hash"], "quote": src["authored_text"]}]}] if nonempty else []
    result = extraction_result(src, principles)
    result.update(extractor_id=extractor_fingerprint(), inference_calls=3)
    return result


def assert_safe_skip(row, calls):
    assert row["state"] == "skipped"
    assert row["reason"] == "source_excluded"
    assert row["inference_calls"] == calls
    assert not {"error_code", "error_type", "attempts", "retryable", "candidates"} & row.keys()
    serialized = json.dumps(row)
    assert "SYNTHETIC_PRIVATE_SOURCE" not in serialized
    assert "SYNTHETIC_MODEL_TEXT" not in serialized


@pytest.mark.parametrize("nonempty", [False, True])
def test_commit_rechecks_exclusion_atomically_even_for_empty_result(stores, nonempty):
    store, reviewer = stores
    src = add(store)
    result = complete(src, nonempty)
    review(reviewer, src, True)
    audit_count = store.db.execute("SELECT count(*) FROM audit").fetchone()[0]

    with pytest.raises(SourceExcludedError) as error:
        store.commit_extraction(src, result)

    assert str(error.value) == "source excluded from philosophy extraction"
    assert store.extraction_record(src) is None
    assert store.principles() == []
    assert store.db.execute("SELECT count(*) FROM audit").fetchone()[0] == audit_count
    review(reviewer, src, False)
    assert [s["source_id"] for s in store.pending_extraction(extractor_id=extractor_fingerprint())] == [src["source_id"]]


def test_queued_exclusion_skips_inference_and_does_not_block_independent_source(stores, monkeypatch):
    store, reviewer = stores
    first, excluded, last = [add(store, suffix) for suffix in ("a", "b", "c")]
    calls = []

    def extract(src, client):
        calls.append(src["source_id"])
        if src["source_id"] == first["source_id"]:
            review(reviewer, excluded, True)
        return complete(src)

    monkeypatch.setattr("tools.cha_philosophy.runner.LocalOllamaClient", lambda **kwargs: object())
    monkeypatch.setattr("tools.cha_philosophy.runner.extract", extract)
    result = distill_pending(store, limit=3)

    assert calls == [first["source_id"], last["source_id"]]
    assert [r["state"] for r in result["results"]] == ["complete", "skipped", "complete"]
    assert_safe_skip(result["results"][1], 0)
    assert store.extraction_record(excluded) is None
    assert result["pending"] == 0

    review(reviewer, excluded, False)
    retried = distill_pending(store, limit=3)
    assert [r["source_id"] for r in retried["results"]] == [excluded["source_id"]]
    assert retried["results"][0]["state"] == "complete"
    assert calls[-1] == excluded["source_id"]


@pytest.mark.parametrize("nonempty", [False, True])
def test_exclusion_during_inference_preserves_retry_then_explicit_clear_retries(stores, monkeypatch, nonempty):
    store, reviewer = stores
    src = add(store)
    store.mark_extraction(src, "failed", attempts=1, retryable=True,
                          next_retry_at="2000-01-01T00:00:00+00:00",
                          extractor_id=extractor_fingerprint())
    previous = store.extraction_record(src)

    def extract(current, client):
        review(reviewer, current, True)
        return complete(current, nonempty)

    monkeypatch.setattr("tools.cha_philosophy.runner.LocalOllamaClient", lambda **kwargs: object())
    monkeypatch.setattr("tools.cha_philosophy.runner.extract", extract)
    result = distill_pending(store)

    assert_safe_skip(result["results"][0], 3)
    assert store.extraction_record(src) == previous
    assert store.principles() == []
    assert result["pending"] == 0

    review(reviewer, src, False)
    assert [s["source_id"] for s in store.pending_extraction(extractor_id=extractor_fingerprint())] == [src["source_id"]]
    monkeypatch.setattr("tools.cha_philosophy.runner.extract", lambda current, client: complete(current, nonempty))
    retried = distill_pending(store)
    assert retried["results"][0]["state"] == "complete"
    assert retried["results"][0]["candidates"] == int(nonempty)
    assert store.extraction_record(src)["status"] == "complete"
    assert all(p["status"] == "candidate" for p in store.principles())


@pytest.mark.parametrize("typed", [False, True])
def test_failed_call_after_exclusion_does_not_consume_attempt_or_leak_output(stores, monkeypatch, typed):
    store, reviewer = stores
    src = add(store)

    def extract(current, client):
        review(reviewer, current, True)
        if typed:
            raise ExtractionError("SYNTHETIC_MODEL_TEXT private malformed output", inference_calls=2)
        raise RuntimeError("SYNTHETIC_MODEL_TEXT private unexpected failure")

    monkeypatch.setattr("tools.cha_philosophy.runner.LocalOllamaClient", lambda **kwargs: object())
    monkeypatch.setattr("tools.cha_philosophy.runner.extract", extract)
    result = distill_pending(store)

    assert_safe_skip(result["results"][0], 2 if typed else None)
    assert store.extraction_record(src) is None
    assert store.principles() == []
    review(reviewer, src, False)
    assert len(list(store.pending_extraction(extractor_id=extractor_fingerprint()))) == 1


def test_backend_outage_after_exclusion_still_stops_remaining_queue(stores, monkeypatch):
    store, reviewer = stores
    first, second = add(store, "a"), add(store, "b")
    calls = []

    def extract(current, client):
        calls.append(current["source_id"])
        review(reviewer, current, True)
        raise ExtractionError("SYNTHETIC_MODEL_TEXT backend body", backend_failure=True, inference_calls=1)

    monkeypatch.setattr("tools.cha_philosophy.runner.LocalOllamaClient", lambda **kwargs: object())
    monkeypatch.setattr("tools.cha_philosophy.runner.extract", extract)
    result = distill_pending(store, limit=2)

    assert calls == [first["source_id"]]
    assert_safe_skip(result["results"][0], 1)
    assert store.extraction_record(first) is None
    assert store.extraction_record(second) is None
    assert result["pending"] == 1
