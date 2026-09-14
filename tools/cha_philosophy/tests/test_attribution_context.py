"""Explicit source attribution survives chunking; it never grants authorship."""
import json

import pytest

from tools.cha_philosophy import distill
from tools.cha_philosophy.store import Store, source_hash


def source(own="주장은 실제로 확인한 근거와 그 한계에 맞추어 작성합니다.", *, body=None):
    row = {"source_id": "fixture:attribution", "platform": "gmail",
           "scope": "synthetic", "author_id": "professor-fixture",
           "authorship": "direct", "status": "active", "authored_text": own,
           "body": own if body is None else body}
    row["source_hash"] = source_hash(row)
    return row


class RecordingClient:
    def __init__(self, answer=None):
        self.calls = []
        self.answer = answer

    def complete(self, messages, schema):
        self.calls.append((messages, schema))
        if self.answer:
            return self.answer(messages, schema)
        return {"outcome": "no_principles", "rationale": "Synthetic explicit empty decision.",
                "principles": []}


def test_signature_attribution_is_present_in_every_chunk_with_exact_body_offsets():
    own = "근거와 한계를 검토하고 구체적인 판단 기준을 설명합니다.\n" * 9
    body = own + "\r\n--\r\nChavis 올림 (Claude Code 대필)\r\n--\r\n"
    src = source(own, body=body)
    client = RecordingClient()
    result = distill.extract(src, client, max_chunk_bytes=100)
    assert len(client.calls) > 2
    for messages, _ in client.calls:
        data = json.loads(messages[-1]["content"])
        context = data["attribution_context"]
        assert context["role"] == "context_only"
        assert context["authorship_verified_by_cues"] is False
        assert context["full_body_supplied"] is False
        assert context["thread_context_supplied"] is False
        assert len(context["spans"]) == 1
        span = context["spans"][0]
        assert body[span["start"]:span["end"]] == span["text"] == "Chavis 올림 (Claude Code 대필)"
    assert result["attribution_cues_supplied"] == 1
    assert "spans" not in result["attribution_context"]


def test_signature_negation_is_not_removed_or_treated_as_authorship_proof():
    src = source(body="교수 직접 문장.\n--\nClaude Code 대필 아님; 본인이 작성했습니다.\n")
    ctx = distill.source_attribution_context(src)
    assert ctx["spans"][0]["text"] == "Claude Code 대필 아님; 본인이 작성했습니다."
    assert ctx["authorship_verified_by_cues"] is False


@pytest.mark.parametrize("marker", ["On Tue, Example wrote:", "-----Original Message-----",
                                     "홍길동님이 작성:"])
def test_quoted_history_byline_does_not_become_current_message_attribution(marker):
    src = source(body="직접 발언.\n> Claude Code 대필\n" + marker + "\nChatGPT: a copied reply\n")
    assert distill.source_attribution_context(src)["spans"] == []


def test_flattened_html_and_mixed_external_byline_remain_unverified_context():
    own = "문헌에서 실제 기여를 확인한 다음 설명하세요.\nEditorial\nAuthor: External Writer\n외부 논설 본문."
    src = source(own)
    ctx = distill.source_attribution_context(src)
    assert [x["text"] for x in ctx["spans"]] == ["Editorial", "Author: External Writer"]
    # A flattened blockquote cannot be distinguished from current text here.
    assert not ctx["authorship_verified_by_cues"]
    result = distill.extract(src, RecordingClient())
    assert result["status"] == "complete"  # Chunk processing, not semantic approval.
    assert result["principles"] == []


def test_unmatched_body_and_private_routing_are_not_forwarded():
    src = source(body="직접 발언.\n학생의 사적인 발언 PRIVATE_CONTEXT\n--\nprivate@example.invalid\n")
    src["recipients"] = ["private@example.invalid"]
    payload = json.dumps(distill.extraction_prompt(src, distill.Chunk(0, 0, len(src["authored_text"]),
                                                                 src["authored_text"]), 1), ensure_ascii=False)
    assert "PRIVATE_CONTEXT" not in payload
    assert "private@example.invalid" not in payload


def test_body_cue_cannot_be_used_as_an_authored_quote():
    src = source(body="직접 발언.\n--\nChavis 올림 (Claude Code 대필)\n")
    def invalid(messages, schema):
        return {"outcome": "principles", "rationale": "Synthetic test.", "principles": [{
            "statement": "도구의 초안을 직접 검토한다.", "domains": ["writing"],
            "evidence": [{"quote": "Chavis 올림 (Claude Code 대필)"}],
            "exceptions": [], "rationale": "Synthetic test."}]}
    with pytest.raises(distill.ExtractionError) as error:
        distill.extract(src, RecordingClient(invalid), max_chunk_attempts=1)
    assert error.value.code == "quote_mismatch"


@pytest.mark.parametrize("body", ["Claude Code 대필 " + "맥락" * 1000,
                                   "\n".join("Author: Writer " + str(i) for i in range(50))])
def test_overflow_requires_review_before_inference_without_partial_success(body):
    client = RecordingClient()
    with pytest.raises(distill.ExtractionError) as error:
        distill.extract(source(body=body), client)
    assert error.value.code == "attribution_context_budget"
    assert error.value.retryable is False
    assert error.value.inference_calls == 0
    assert not client.calls
    assert body not in str(error.value)


def test_actual_json_budget_reduces_chunks_preserves_unicode_and_fits_retry():
    own = '주장은 근거로 설명합니다. "quoted"\\\t\n' * 180
    src = source(own, body=own + "\n--\nClaude Code 대필 아님; " + "작성 맥락 " * 55)
    seen = {}
    def answer(messages, schema):
        payload = json.loads(messages[-1]["content"])
        size = sum(len(x["content"].encode()) for x in messages)
        size += len(json.dumps(schema, ensure_ascii=False).encode()) + distill.OUTPUT_TOKENS + 1024
        assert size <= distill.CONTEXT_TOKENS
        idx = payload["chunk_index"]
        seen[idx] = seen.get(idx, 0) + 1
        if seen[idx] == 1:
            return {}  # Exercise the actual retry suffix, not just an estimate.
        return {"outcome": "no_principles", "rationale": "Synthetic empty decision.", "principles": []}
    client = RecordingClient(answer)
    result = distill.extract(src, client)
    assert result["effective_chunk_bytes"] < distill.MAX_CHUNK_BYTES
    chunks = [json.loads(m[-1]["content"])["authored_text"] for m, _ in client.calls[::2]]
    assert "".join(chunks) == own
    assert all(count == 2 for count in seen.values())


def test_body_attribution_change_rejects_old_hash_before_inference():
    src = source()
    src["body"] += "\n--\nClaude Code 대필"
    client = RecordingClient()
    with pytest.raises(distill.ExtractionError) as error:
        distill.extract(src, client)
    assert error.value.code == "source_hash_mismatch"
    assert not client.calls


def test_changed_attribution_policy_requeues_prior_empty_completion(tmp_path, monkeypatch):
    store = Store(tmp_path / "private")
    src = source()
    store.upsert(src)
    current = store.get_source(src["source_id"])
    old_policy = distill.extractor_fingerprint()
    store.commit_extraction(current, distill.extract(current, RecordingClient()))
    assert list(store.pending_extraction(extractor_id=old_policy)) == []
    monkeypatch.setattr(distill, "MAX_ATTRIBUTION_CONTEXT_BYTES", distill.MAX_ATTRIBUTION_CONTEXT_BYTES + 1)
    new_policy = distill.extractor_fingerprint()
    assert new_policy != old_policy
    assert [x["source_id"] for x in store.pending_extraction(extractor_id=new_policy)] == [src["source_id"]]
    assert store.extraction_record(current)["details"]["extractor_id"] == old_policy
    store.close()


def test_complete_ledger_preserves_context_limits_without_copying_body(tmp_path):
    store = Store(tmp_path / "private")
    src = source(body="직접 발언.\n--\nChavis 올림 (Claude Code 대필)\n")
    store.upsert(src, verified_remote=True)
    current = store.get_source(src["source_id"])
    result = distill.extract(current, RecordingClient())
    store.commit_extraction(current, result)
    details = store.extraction_record(current)["details"]
    assert details["attribution_cues_supplied"] == 1
    assert details["attribution_context"]["thread_context_supplied"] is False
    assert "Claude Code 대필" not in json.dumps(details, ensure_ascii=False)
    assert store.principles() == []
    store.close()
