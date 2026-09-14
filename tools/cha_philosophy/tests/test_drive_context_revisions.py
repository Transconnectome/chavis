"""Referenced Drive passages and regions are part of reviewed source identity."""
from copy import deepcopy

import pytest

import tools.cha_philosophy.store as store_module
from tools.cha_philosophy.connectors import normalize_drive_comment
from tools.cha_philosophy.store import Store, digest, source_hash
from tools.cha_philosophy.task import audit_task_output, prepare_task


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "synthetic-private")
    yield value
    value.close()


def source(kind="comment", *, with_context=True):
    file = {"id": "synthetic-file", "name": "Synthetic research draft",
            "_account_id": "professor@example.test"}
    comment = {
        "id": "synthetic-comment", "content": "이 결과도 같은 메인 표에 포함해 주세요.",
        "author": {"me": True}, "createdTime": "2026-09-01T00:00:00Z",
        "modifiedTime": "2026-09-01T00:00:00Z", "resolved": False,
        "replies": [{"id": "synthetic-reply", "content": "같은 기준으로 이 결과도 함께 보고해 주세요.",
                     "author": {"me": True}, "createdTime": "2026-09-01T00:00:00Z",
                     "modifiedTime": "2026-09-01T00:00:00Z"}],
    }
    if with_context:
        comment.update(anchor='{"region":"final-benchmark-table"}',
                       quotedFileContent={"mimeType": "text/plain",
                                          "value": "Final results from five benchmark tasks."})
    return normalize_drive_comment(file, comment, True)[0 if kind == "comment" else 1]


def legacy_hash(value):
    """The persisted representation before referenced context was bound."""
    material = {key: value.get(key, "") for key in (
        "body", "authored_text", "author_id", "authorship", "status", "scope", "url", "title", "created_at")}
    material["attribution"] = {key: value.get("metadata", {}).get(key) for key in (
        "authorship_basis", "sent_by", "authored_by", "approved_by", "sent_vs_authored", "last_edited_at")}
    return digest(material)


def activate(store, value):
    store.upsert(value, verified_remote=True)
    current = store.get_source(value["source_id"])
    pid = store.add_principle({
        "statement": "최종 평가한 비교 과제의 결과를 모두 보고한다.", "domains": ["review"],
        "evidence": [{"source_id": current["source_id"], "source_hash": current["source_hash"],
                      "quote": current["authored_text"]}],
    })
    store.review(pid, "evidence_supported", "synthetic-reviewer", "Reviewed synthetic referenced context")
    return pid


def output(pid, sid):
    content = "최종 평가한 비교 과제의 결과를 모두 보고한다."
    return {"content": content,
            "applications": [{"principle_id": pid, "applied_to": content,
                              "rationale": "Synthetic application of the reviewed interpretation"}],
            "claims": [{"text": content, "source_ids": [sid], "claim_type": "inference"}],
            "uncertainties": []}


@pytest.mark.parametrize("kind", ["comment", "reply"])
@pytest.mark.parametrize("mutation", ["quoted_value", "quoted_mime", "anchor", "remove_quote", "remove_anchor"])
def test_referenced_context_change_invalidates_principle_and_prepared_task(store, kind, mutation):
    original = source(kind)
    pid = activate(store, original)
    old_bundle = store.bundle("review", "최종 평가 결과")
    task_bundle = prepare_task(store, "review", "최종 평가 결과를 검토해주세요.", [])
    artifact = output(pid, original["source_id"])
    assert audit_task_output(store, task_bundle, artifact)["status"] == "structural_valid"

    changed = deepcopy(original)
    metadata = changed["metadata"]
    if mutation == "quoted_value":
        metadata["quoted_file_content"]["value"] = "A preliminary exploratory condition."
    elif mutation == "quoted_mime":
        metadata["quoted_file_content"]["mimeType"] = "text/html"
    elif mutation == "anchor":
        metadata["anchor"] = '{"region":"preliminary-appendix"}'
    elif mutation == "remove_quote":
        metadata.pop("quoted_file_content")
    else:
        metadata.pop("anchor")

    assert changed["body"] == original["body"]
    assert changed["modified_at"] == original["modified_at"]
    assert store.upsert(changed, verified_remote=True)
    assert store.get_principle(pid)["status"] == "stale"
    assert store.validate_bundle(old_bundle)
    assert audit_task_output(store, task_bundle, artifact)["status"] == "invalid"
    assert not prepare_task(store, "review", "최종 평가 결과를 검토해주세요.", [])["principles"]


@pytest.mark.parametrize("kind", ["comment", "reply"])
def test_absence_is_bound_and_adding_context_invalidates(store, kind):
    original = source(kind, with_context=False)
    assert source_hash(original) != legacy_hash(original)
    without_keys = deepcopy(original)
    without_keys["metadata"].pop("quoted_file_content")
    without_keys["metadata"].pop("anchor")
    assert source_hash(without_keys) == source_hash(original)

    pid = activate(store, original)
    assert not store.upsert(without_keys, verified_remote=True)
    assert store.get_principle(pid)["status"] == "evidence_supported"
    assert store.upsert(source(kind), verified_remote=True)
    assert store.get_principle(pid)["status"] == "stale"


@pytest.mark.parametrize("kind", ["comment", "reply"])
@pytest.mark.parametrize("with_context", [False, True])
def test_legacy_rows_fail_closed_without_migration_or_automatic_reapproval(store, monkeypatch, kind, with_context):
    original = source(kind, with_context=with_context)
    with monkeypatch.context() as legacy:
        legacy.setattr(store_module, "source_hash", legacy_hash)
        pid = activate(store, original)
        bundle = store.bundle("review", "최종 평가 결과")
        task_bundle = prepare_task(store, "review", "최종 평가 결과를 검토해주세요.", [])
    prior = store.get_source(original["source_id"])
    evidence = store.get_principle(pid)["evidence"]
    changes = store.db.total_changes

    assert store.validate_evidence(evidence) == ["source integrity mismatch"]
    assert "source integrity mismatch" in store.validate_bundle(bundle)
    assert audit_task_output(store, task_bundle, output(pid, original["source_id"]))["status"] == "invalid"
    assert not prepare_task(store, "review", "최종 평가 결과를 검토해주세요.", [])["principles"]
    with pytest.raises(ValueError, match="source integrity mismatch"):
        store.review(pid, "evidence_supported", "synthetic-reviewer", "Cannot reuse the legacy approval")
    assert store.db.total_changes == changes
    assert store.get_source(original["source_id"])["source_hash"] == prior["source_hash"]
    assert store.get_principle(pid)["status"] == "evidence_supported"  # Stored history, ineligible for use.

    assert store.upsert(original)  # Explicit local rehash is not a remote read or a new approval.
    assert store.get_source(original["source_id"])["verified_at"] == prior["verified_at"]
    assert store.get_principle(pid)["status"] == "stale"
    assert store.validate_evidence(evidence) == ["source revision mismatch"]


@pytest.mark.parametrize("kind", ["comment", "reply"])
def test_identical_fetch_and_operational_metadata_keep_review_eligible(store, kind):
    original = source(kind)
    pid = activate(store, original)
    assert not store.upsert(deepcopy(original), verified_remote=True)
    changed = deepcopy(original)
    changed["metadata"]["quoted_file_content"] = dict(reversed(list(
        changed["metadata"]["quoted_file_content"].items())))
    changed["modified_at"] = "2026-09-02T00:00:00Z"
    changed["metadata"].update(resolved=True, last_sync_attempt="synthetic-later-attempt")
    assert source_hash(changed) == source_hash(original)
    assert not store.upsert(changed, verified_remote=True)
    assert store.get_principle(pid)["status"] == "evidence_supported"
    assert not store.validate_evidence(store.get_principle(pid)["evidence"])
    assert len(prepare_task(store, "review", "최종 평가 결과를 검토해주세요.", [])["principles"]) == 1


@pytest.mark.parametrize("platform,kind", [("gmail", "comment"), ("teams", "reply"), ("drive", "document")])
def test_unrelated_source_hash_formats_are_unchanged(platform, kind):
    value = source()
    value["platform"] = platform
    value["metadata"]["source_kind"] = kind
    assert source_hash(value) == legacy_hash(value)
