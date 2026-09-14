"""Synthetic retry, fairness and candidate-only orchestration tests."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tools.cha_philosophy.distill import ExtractionError, DEFAULT_MODEL, extractor_fingerprint
from tools.cha_philosophy.runner import distill_pending
from tools.cha_philosophy.store import Store

AT = datetime(2026, 9, 12, tzinfo=timezone.utc)


def synthetic_source(sid, text="주장의 강도는 자료가 뒷받침하는 수준에 맞춥니다."):
    return {"source_id": sid, "platform": "fixture", "scope": "synthetic",
            "author_id": "verified-professor-fixture", "authorship": "direct",
            "body": text or "Forwarded context only", "authored_text": text,
            "modified_at": AT.isoformat(), "status": "active"}


def valid_inference(messages, schema):
    data = json.loads(messages[-1]["content"])
    return {"outcome": "principles", "rationale": "발언에 명시된 근거 중심의 판단 기준입니다.",
            "principles": [{"statement": "주장의 강도를 근거 수준에 맞춘다.",
                            "domains": ["review"], "exceptions": [],
                            "rationale": "발언에 판단의 기준이 명시되어 있습니다.",
                            "evidence": [{"quote": data["authored_text"]}]}]}


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.client = Mock()
        self.client.model = DEFAULT_MODEL
        self.client.complete.side_effect = valid_inference
        self.client_patch = patch("tools.cha_philosophy.runner.LocalOllamaClient", return_value=self.client)
        self.client_patch.start()
        self.clock_patch = patch("tools.cha_philosophy.runner._now", return_value=AT)
        self.clock = self.clock_patch.start()

    def tearDown(self):
        self.clock_patch.stop()
        self.client_patch.stop()
        self.store.close()
        self.tmp.cleanup()

    def add(self, sid, text=None):
        self.store.upsert(synthetic_source(sid) if text is None else synthetic_source(sid, text))
        return self.store.get_source(sid)

    def test_empty_source_is_held_without_starving_later_source(self):
        empty = self.add("fixture:a", "")
        full = self.add("fixture:b")
        result = distill_pending(self.store, limit=2)
        self.assertEqual([r["state"] for r in result["results"]], ["failed", "complete"])
        self.assertEqual(result["results"][0]["error_code"], "source_empty")
        self.assertEqual(result["held"], 1)
        self.assertEqual(self.client.complete.call_count, 1)
        self.assertEqual(self.store.extraction_record(empty)["status"], "failed")
        self.assertEqual(self.store.extraction_record(full)["status"], "complete")
        self.assertEqual(distill_pending(self.store, limit=2)["results"], [])
        self.assertEqual(self.client.complete.call_count, 1)
        self.assertTrue(all(p["status"] == "candidate" for p in self.store.principles()))

    def test_malformed_response_backoff_and_bounded_per_revision_retries(self):
        src = self.add("fixture:a")
        self.client.complete.return_value = {}
        self.client.complete.side_effect = None
        first = distill_pending(self.store, limit=1)
        self.assertEqual(self.client.complete.call_count, 2)
        self.assertEqual(first["deferred"], 1)
        self.assertEqual(first["results"][0]["error_code"], "schema_fields")
        self.assertEqual(first["results"][0]["attempts"], 1)
        self.assertEqual(distill_pending(self.store)["results"], [])
        self.clock.return_value = AT + timedelta(seconds=300)
        second = distill_pending(self.store)
        self.assertEqual(second["results"][0]["attempts"], 2)
        self.clock.return_value = AT + timedelta(seconds=900)
        third = distill_pending(self.store)
        self.assertEqual(third["held"], 1)
        self.assertEqual(self.client.complete.call_count, 6)
        self.clock.return_value = AT + timedelta(days=30)
        self.assertEqual(distill_pending(self.store)["results"], [])
        self.assertEqual(self.store.principles(), [])
        self.assertEqual(self.store.extraction_record(src)["status"], "failed")
        # A source edit creates a new revision; an old failure cannot hold it.
        self.store.upsert(synthetic_source("fixture:a", "이전 결론은 새 근거에 맞추어 수정합니다."))
        self.client.complete.side_effect = valid_inference
        revised = distill_pending(self.store)
        self.assertEqual(revised["results"][0]["state"], "complete")

    def test_untouched_sources_precede_due_retries(self):
        old = self.add("fixture:a")
        new = self.add("fixture:b")
        self.store.mark_extraction(old, "failed", attempts=1, retryable=True,
                                   extractor_id=extractor_fingerprint(),
                                   next_retry_at=(AT - timedelta(minutes=10)).isoformat())
        result = distill_pending(self.store, limit=1)
        self.assertEqual(result["results"][0]["source_id"], new["source_id"])
        self.assertEqual(result["ready"], 1)
        self.assertEqual(self.store.extraction_record(old)["status"], "failed")

    def test_backend_outage_stops_without_consuming_other_sources_attempts(self):
        first = self.add("fixture:a")
        second = self.add("fixture:b")
        self.client.complete.side_effect = ExtractionError(
            "unlogged private backend body", code="backend_transport", backend_failure=True)
        result = distill_pending(self.store, limit=2)
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(self.client.complete.call_count, 1)
        self.assertEqual(self.store.extraction_record(first)["status"], "failed")
        self.assertIsNone(self.store.extraction_record(second))
        self.assertNotIn("unlogged private", json.dumps(result))
        self.assertNotIn("unlogged private", json.dumps(self.store.extraction_record(first)))

    def test_store_failure_does_not_report_success_or_leak_message(self):
        src = self.add("fixture:a")
        with patch.object(self.store, "commit_extraction", side_effect=ValueError("private quote value")):
            result = distill_pending(self.store, limit=1)
        self.assertEqual(result["results"][0]["state"], "failed")
        self.assertEqual(result["results"][0]["error_code"], "store_commit_failed")
        self.assertEqual(result["results"][0]["inference_calls"], 1)
        self.assertEqual(self.store.principles(), [])
        self.assertEqual(self.store.extraction_record(src)["status"], "failed")
        self.assertNotIn("private quote", json.dumps(result))

    def test_same_policy_skips_completion_and_prompt_change_reprocesses_empty_result(self):
        src = self.add("fixture:a")
        self.client.complete.side_effect = None
        self.client.complete.return_value = {
            "outcome": "no_principles", "principles": [],
            "rationale": "현재 구간에는 일반화할 수 있는 원칙이 없습니다."}
        first = distill_pending(self.store)
        old_id = first["extractor_id"]
        self.assertEqual(first["results"][0]["candidates"], 0)
        self.assertEqual(self.store.extraction_record(src)["details"]["extractor_id"], old_id)
        self.assertEqual(list(self.store.pending_extraction(extractor_id=old_id)), [])
        self.assertEqual(distill_pending(self.store)["results"], [])
        self.assertEqual(self.client.complete.call_count, 1)
        self.client.complete.side_effect = valid_inference
        with patch("tools.cha_philosophy.distill.SYSTEM_PROMPT", "revised synthetic extractor prompt"):
            new_id = extractor_fingerprint()
            self.assertNotEqual(new_id, old_id)
            self.assertEqual(len(list(self.store.pending_extraction(extractor_id=new_id))), 1)
            # Merely inspecting changed-policy eligibility cannot rewrite history.
            self.assertEqual(self.store.extraction_record(src)["details"]["extractor_id"], old_id)
            second = distill_pending(self.store)
        self.assertEqual(second["results"][0]["candidates"], 1)
        self.assertEqual(self.store.extraction_record(src)["details"]["extractor_id"], new_id)
        events = [json.loads(r[0]) for r in self.store.db.execute(
            "SELECT details FROM audit WHERE action='extraction_complete' ORDER BY seq")]
        self.assertEqual(len(events), 2)
        self.assertEqual(events[-1]["previous_extractor_id"], old_id)
        self.assertEqual(events[-1]["previous_status"], "complete")
        self.assertEqual(self.store.principles()[0]["status"], "candidate")

    def test_old_or_missing_policy_does_not_preserve_held_failure(self):
        for fingerprint in (None, "a" * 64):
            with self.subTest(fingerprint=fingerprint):
                src = self.add("fixture:" + str(fingerprint))
                self.store.mark_extraction(src, "failed", attempts=3, held=True, retryable=False,
                                           extractor_id=fingerprint)
        result = distill_pending(self.store)
        self.assertEqual(len(result["results"]), 2)
        self.assertTrue(all(r["state"] == "complete" for r in result["results"]))
        self.assertEqual(result["held"], 0)

    def test_old_policy_failure_retries_from_attempt_one_after_change(self):
        src = self.add("fixture:a")
        self.store.mark_extraction(src, "failed", attempts=3, held=True, retryable=False,
                                   extractor_id="a" * 64)
        self.client.complete.side_effect = None
        self.client.complete.return_value = {}
        result = distill_pending(self.store)
        details = self.store.extraction_record(src)["details"]
        self.assertEqual(result["results"][0]["state"], "failed")
        self.assertEqual(details["attempts"], 1)
        self.assertFalse(details["held"])
        self.assertEqual(details["extractor_id"], extractor_fingerprint())

    def test_legacy_complete_record_without_policy_becomes_eligible(self):
        src = self.add("fixture:a")
        self.store.mark_extraction(src, "complete", chunks_total=1, chunks_processed=1)
        self.assertEqual(list(self.store.pending_extraction()), [])
        self.assertEqual(len(list(self.store.pending_extraction(extractor_id=extractor_fingerprint()))), 1)
        result = distill_pending(self.store)
        self.assertEqual(result["results"][0]["state"], "complete")
        self.assertEqual(result["pending"], 0)

    def test_model_change_reprocesses_and_same_model_keeps_completion(self):
        self.add("fixture:a")
        distill_pending(self.store)
        other = "synthetic-local-model:v2"
        self.client.model = other
        second = distill_pending(self.store, model=other)
        self.assertEqual(second["results"][0]["state"], "complete")
        self.assertEqual(second["extractor_id"], extractor_fingerprint(other))
        self.assertEqual(distill_pending(self.store, model=other)["results"], [])

    def test_inference_policy_mismatch_cannot_complete(self):
        src = self.add("fixture:a")
        self.client.model = "unexpected-local-model"
        result = distill_pending(self.store)
        self.assertEqual(result["results"][0]["error_code"], "policy_mismatch")
        self.assertEqual(self.store.principles(), [])
        self.assertEqual(self.store.extraction_record(src)["status"], "failed")

    def test_limit_rejects_boolean_and_noninteger(self):
        for value in (True, 0, -1, 1.5):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    distill_pending(self.store, limit=value)


if __name__ == "__main__":
    unittest.main()
