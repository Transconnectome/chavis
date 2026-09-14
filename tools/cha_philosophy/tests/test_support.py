"""Reviewed evidence additions preserve interpretation and fail atomically."""
from copy import deepcopy
import json

import pytest

from tools.cha_philosophy.cli import main
from tools.cha_philosophy.store import Store, digest


@pytest.fixture
def store(tmp_path):
    value=Store(tmp_path / "private")
    yield value
    value.close()


def evidence_source(store, suffix, *, authorship="direct"):
    quote=f"Synthetic source {suffix}: support every conclusion with the observed evidence."
    sid="synthetic-drive:"+suffix
    store.upsert({"source_id":sid,"platform":"drive","scope":"synthetic",
                  "authorship":authorship,"author_id":"synthetic-professor",
                  "status":"active","body":quote,"authored_text":quote},verified_remote=True)
    source=store.get_source(sid)
    return {"source_id":sid,"source_hash":source["source_hash"],"quote":quote}


def supported(store):
    evidence=evidence_source(store,"original")
    pid=store.add_principle({"statement":"Evidence bounds the conclusion.",
        "domains":["writing","evaluation","review"],"exceptions":["Report uncertain scope explicitly."],
        "rationale":"Synthetic original interpretation.","evidence":[evidence]})
    store.review(pid,"evidence_supported","original-reviewer","Synthetic original review.")
    return pid,evidence


def records(store):
    return {table:[tuple(row) for row in store.db.execute("SELECT * FROM "+table)]
            for table in ("principles","evidence","audit")}


def test_support_preserves_interpretation_records_review_and_invalidates_old_bundle(store):
    pid,original=supported(store)
    added=evidence_source(store,"additional")
    prior=store.get_principle(pid)
    old_bundle=store.bundle("review","evidence")
    reason="Reviewed the exact passage: "+added["quote"]
    result=store.add_support(pid,[added],"support-reviewer",reason)
    current=store.get_principle(pid)
    for field in ("statement","domains","exceptions","rationale","status","review","conflicts_with"):
        assert current[field]==prior[field]
    assert current["evidence"]==[original,added]
    assert current["support_reviews"][0]["reason"]==reason
    assert result["added_evidence_count"]==1 and result["evidence_count"]==2
    assert result["prior_principle_digest"]==digest(prior)
    assert result["new_principle_digest"]==digest(current)
    audit=json.loads(store.db.execute("SELECT details FROM audit WHERE action='principle_support'").fetchone()[0])
    assert audit["reviewer"]=="support-reviewer"
    assert audit["reason_hash"]==digest(reason) and "reason" not in audit
    assert audit["prior_principle_digest"]==digest(prior)
    assert audit["new_principle_digest"]==digest(current)
    assert added["quote"] not in json.dumps(records(store)["audit"])
    assert store.validate_bundle(old_bundle)
    assert not store.validate_bundle(store.bundle("review","evidence"))


def test_duplicate_source_quote_pairs_are_idempotent_without_review_or_audit_writes(store):
    pid,original=supported(store)
    added=evidence_source(store,"additional")
    supplied=[original,added,deepcopy(added)]
    frozen=deepcopy(supplied)
    first=store.add_support(pid,supplied,"reviewer","Checked the additional support.")
    assert first["added_evidence_count"]==1 and supplied==frozen
    before=records(store);changes=store.db.total_changes
    second=store.add_support(pid,[added,original,added],"other-reviewer","Repeated request.")
    assert second["changed"] is False and second["added_evidence_count"]==0
    assert second["prior_principle_digest"]==second["new_principle_digest"]
    assert records(store)==before and store.db.total_changes==changes


@pytest.mark.parametrize("existing_duplicate",[False,True])
def test_wrong_hash_rolls_back_all_support_even_for_duplicate_identity(store,existing_duplicate):
    pid,original=supported(store)
    valid=evidence_source(store,"valid")
    invalid=dict(original if existing_duplicate else evidence_source(store,"invalid"),source_hash="wrong-hash")
    before=records(store)
    with pytest.raises(ValueError,match="revision mismatch"):
        store.add_support(pid,[valid,invalid],"reviewer","Must reject the whole batch.")
    assert records(store)==before


def test_combined_evidence_is_rechecked_inside_transaction_and_failure_rolls_back(store,monkeypatch):
    pid,original=supported(store)
    added=evidence_source(store,"additional")
    before=records(store)
    original_validate=store.validate_evidence
    calls=[]
    def changed_between_checks(evidence):
        calls.append((store.db.in_transaction,deepcopy(evidence)))
        if len(calls)==2:
            return ["source revision mismatch"]
        return original_validate(evidence)
    monkeypatch.setattr(store,"validate_evidence",changed_between_checks)
    with pytest.raises(ValueError,match="revision mismatch"):
        store.add_support(pid,[added],"reviewer","Check within the transaction.")
    assert calls==[(False,[original,added]),(True,[original,added])]
    assert records(store)==before


def test_failure_after_evidence_and_principle_writes_rolls_back_everything(store,monkeypatch):
    pid,_=supported(store)
    added=evidence_source(store,"additional")
    before=records(store)
    def fail(*args,**kwargs):
        raise RuntimeError("synthetic audit failure")
    monkeypatch.setattr(store,"_audit",fail)
    with pytest.raises(RuntimeError,match="synthetic audit failure"):
        store.add_support(pid,[added],"reviewer","Atomic update required.")
    assert records(store)==before


def test_held_out_source_cannot_become_additional_support(store):
    pid,_=supported(store)
    added=evidence_source(store,"held")
    store.hold_for_evaluation(added["source_id"],"Keep this independent of principle development.")
    before=records(store)
    with pytest.raises(ValueError,match="reserved for evaluation"):
        store.add_support(pid,[added],"reviewer","Must not use held-out evidence.")
    assert records(store)==before


@pytest.mark.parametrize("state",["candidate","professor_confirmed","stale","disputed","rejected","superseded"])
def test_support_rejects_every_status_except_evidence_supported(store,state):
    pid,_=supported(store)
    added=evidence_source(store,"additional")
    if state=="candidate":
        prior=store.get_principle(pid)
        pid=store.add_principle({**prior,"principle_id":"synthetic-candidate"})
    else:
        store.review(pid,state,"reviewer","Synthetic status fixture.",professor_attestation=state=="professor_confirmed")
    before=records(store)
    with pytest.raises(ValueError,match="requires an evidence_supported"):
        store.add_support(pid,[added],"reviewer","Must not change eligibility.")
    assert records(store)==before


@pytest.mark.parametrize("authorship",["context","unverified"])
def test_support_requires_direct_source_authorship(store,authorship):
    pid,_=supported(store)
    added=evidence_source(store,"other-speaker",authorship=authorship)
    before=records(store)
    with pytest.raises(ValueError,match="direct authorship"):
        store.add_support(pid,[added],"reviewer","Not professor evidence.")
    assert records(store)==before


def test_source_revocation_erases_free_text_support_review_reason(store):
    pid,_=supported(store)
    added=evidence_source(store,"additional")
    store.add_support(pid,[added],"reviewer","Direct passage: "+added["quote"])
    store.revoke(added["source_id"])
    current=store.get_principle(pid)
    assert current["status"]=="stale" and "support_reviews" not in current
    assert added["quote"] not in json.dumps(records(store))


def test_support_cli_accepts_evidence_array_without_echoing_quotes(store,tmp_path,capsys):
    pid,_=supported(store)
    added=evidence_source(store,"additional")
    path=tmp_path/"support.json"
    path.write_text(json.dumps([added]),encoding="utf-8")
    assert main(["--home",str(store.home),"support",pid,str(path),
                 "--reviewer","cli-reviewer","--reason","Checked scope and meaning."])==0
    output=capsys.readouterr().out
    assert json.loads(output)["added_evidence_count"]==1
    assert added["quote"] not in output


def test_support_cli_rejects_duplicate_json_keys_before_mutation(store,tmp_path,capsys):
    pid,_=supported(store)
    path=tmp_path/"ambiguous.json"
    path.write_text('[{"source_id":"first","source_id":"second","source_hash":"hash","quote":"quote"}]')
    before=records(store)
    assert main(["--home",str(store.home),"support",pid,str(path),
                 "--reviewer","cli-reviewer","--reason","Strict parsing required."])==1
    assert json.loads(capsys.readouterr().out)["error_code"]=="ambiguous_task_json"
    assert records(store)==before
