"""A reviewed attribution problem cannot re-enter philosophy through refetch."""
import json

import pytest

from tools.cha_philosophy.cli import main
from tools.cha_philosophy.store import Store
from tools.cha_philosophy.tests.test_store import source, candidate, activate


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path / "private")
    yield store
    store.close()


def exclude(store, src, decision=True, reason="Explicit assistant self-identification, independently read."):
    return store.review_source_exclusion(src["source_id"], source_hash=src["source_hash"], exclude=decision,
                                         reviewer="synthetic reviewer", reason=reason)


def test_exclusion_preserves_original_and_sibling_but_blocks_extraction_and_evidence(store):
    store.upsert(source(), verified_remote=True)
    src = store.get_source(source()["source_id"])
    sibling = source(source_id="gmail/test/thread/sibling", body="This is a separate research instruction.",
                     authored_text="This is a separate research instruction.")
    store.upsert(sibling, verified_remote=True)
    proposal = candidate(store)
    exclude(store, src)
    assert store.get_source(src["source_id"]) == src
    assert [s["source_id"] for s in store.pending_extraction()] == [sibling["source_id"]]
    assert store.search("주장") == []
    assert any(s["source_id"] == src["source_id"] for s in store.search("주장", direct=False))
    assert store.status()["evaluation_reserved_families"] == 0
    with pytest.raises(ValueError, match="excluded from philosophy"):
        store.add_principle(proposal)


def test_existing_principle_and_receipt_cannot_use_excluded_evidence(store):
    pid = activate(store)
    bundle = store.bundle("review", "근거")
    src = store.get_source(source()["source_id"])
    exclude(store, src)
    assert store.get_principle(pid)["status"] == "stale"
    assert store.validate_bundle(bundle)
    assert not store.bundle("review", "근거")["principles"]
    with pytest.raises(ValueError, match="excluded from philosophy"):
        store.review(pid, "evidence_supported", "another reviewer", "attempt to bypass exclusion")


def test_refetch_new_revision_cannot_clear_exclusion_and_clear_is_not_activation(store):
    pid = activate(store)
    src = store.get_source(source()["source_id"])
    exclude(store, src)
    store.upsert(source(), verified_remote=True)
    assert not store.source_exclusion(src["source_id"])["revision_changed_since_review"]
    changed = source(body="새로운 검토 지시를 전달합니다.", authored_text="새로운 검토 지시를 전달합니다.",
                     metadata={"source_exclusion": False, "model_says_approved": True})
    store.upsert(changed, verified_remote=True)
    current = store.get_source(src["source_id"])
    assert store.source_exclusion(src["source_id"])["revision_changed_since_review"]
    assert list(store.pending_extraction()) == []
    with pytest.raises(ValueError, match="revision changed"):
        exclude(store, src, False)
    assert store.source_exclusion(src["source_id"])
    result = exclude(store, current, False, "Current revision independently checked; it is a new attributable instruction.")
    assert result["principles_automatically_activated"] is False
    assert store.get_principle(pid)["status"] == "stale"
    assert len(list(store.pending_extraction())) == 1


def test_delayed_exclusion_cannot_invalidate_unread_new_revision(store):
    store.upsert(source(), verified_remote=True)
    old = store.get_source(source()["source_id"])
    store.upsert(source(body="New material differs.", authored_text="New material differs."), verified_remote=True)
    with pytest.raises(ValueError, match="revision changed"):
        exclude(store, old)
    assert store.source_exclusions() == []


def test_source_revocation_erases_free_text_reason_and_never_reopens_evidence(store):
    store.upsert(source(), verified_remote=True)
    src = store.get_source(source()["source_id"])
    exclude(store, src, reason="SYNTHETIC_PRIVATE_EXCERPT reason for withholding.")
    store.revoke(src["source_id"])
    assert "SYNTHETIC_PRIVATE_EXCERPT" not in store.source_exclusion(src["source_id"])["reason"]
    assert not any("SYNTHETIC_PRIVATE_EXCERPT" in row[0] for row in store.db.execute("SELECT details FROM audit"))
    with pytest.raises(ValueError, match="active source"):
        exclude(store, store.get_source(src["source_id"]), False)


def test_review_reason_redacts_credentials_but_preserves_review_context(store):
    store.upsert(source(), verified_remote=True)
    src = store.get_source(source()["source_id"])
    exclude(store, src, reason="Forwarded meeting credentials. Passcode: SYNTHETIC_MEETING_SECRET")
    reason = store.source_exclusion(src["source_id"])["reason"]
    assert "SYNTHETIC_MEETING_SECRET" not in reason
    assert "Forwarded meeting credentials" in reason


def test_cli_persists_explicit_exclusion_and_clearing_across_connections(store, capsys):
    store.upsert(source(), verified_remote=True)
    src = store.get_source(source()["source_id"])
    args = ["--home", str(store.home)]
    review_args = [src["source_id"], "--source-hash", src["source_hash"], "--reviewer", "synthetic reviewer",
                   "--reason", "Explicitly reviewed attribution of current source."]
    assert main(args + ["exclude-source"] + review_args) == 0
    assert json.loads(capsys.readouterr().out)["excluded_from_philosophy"] is True
    assert main(args + ["source-exclusions"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 1
    assert main(args + ["clear-source-exclusion"] + review_args) == 0
    assert json.loads(capsys.readouterr().out)["excluded_from_philosophy"] is False
    assert store.source_exclusions() == []


def test_failed_outer_transaction_rolls_back_exclusion_and_invalidation(store):
    pid = activate(store)
    src = store.get_source(source()["source_id"])
    with pytest.raises(RuntimeError):
        with store.transaction():
            exclude(store, src)
            raise RuntimeError("synthetic interruption")
    assert store.source_exclusion(src["source_id"]) is None
    assert store.get_principle(pid)["status"] == "evidence_supported"
