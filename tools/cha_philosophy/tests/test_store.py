from copy import deepcopy

import pytest

from tools.cha_philosophy.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "private")
    yield s
    s.close()


def source(**changes):
    return {"source_id":"gmail/test/thread/message", "platform":"gmail", "scope":"test-lab",
            "author_id":"professor@example.org", "authorship":"direct", "status":"active",
            "title":"연구 피드백", "body":"주장보다 근거를 먼저 확인하세요.\n> 동료의 인용문입니다.",
            "authored_text":"주장보다 근거를 먼저 확인하세요.", "created_at":"2026-09-01T00:00:00Z",
            "modified_at":"2026-09-01T00:00:00Z", "url":"https://example.org/evidence", **changes}


def candidate(store, **changes):
    s = next(store.sources(direct=True))
    return {"statement":"연구 주장을 작성하기 전에 근거를 확인한다.", "domains":["writing","review"],
            "evidence":[{"source_id":s["source_id"],"source_hash":s["source_hash"],
                         "quote":s["authored_text"]}], **changes}


def activate(store):
    store.upsert(source(),verified_remote=True)
    pid = store.add_principle(candidate(store))
    store.review(pid,"evidence_supported","test independent reviewer","quote supports scoped interpretation")
    return pid


def test_candidate_cannot_self_promote(store):
    store.upsert(source(),verified_remote=True)
    pid = store.add_principle(candidate(store, status="professor_confirmed"))
    assert store.get_principle(pid)["status"] == "candidate"
    assert store.bundle("writing","근거")["principles"] == []
    with pytest.raises(ValueError, match="attestation"):
        store.review(pid,"professor_confirmed","agent","I assume yes")


def test_evaluation_hold_is_family_wide_and_does_not_fake_extraction_completion(store):
    metadata={"account_id":"professor@example.org","thread_id":"test-thread"}
    store.upsert(source(metadata=metadata),verified_remote=True)
    store.hold_for_evaluation(source()["source_id"],"unseen reference feedback screening")
    p=candidate(store)
    with pytest.raises(ValueError,match="reserved for evaluation"):
        store.add_principle(p)
    store.upsert(source(source_id="gmail/test/thread/new-reply",metadata=metadata),verified_remote=True)
    assert list(store.pending_extraction())==[]
    assert store.db.execute("SELECT count(*) FROM extraction").fetchone()[0]==0
    assert len(list(store.sources(direct=True)))==2
    store.release_evaluation(source()["source_id"],"evaluation complete; available for future learning")
    assert len(list(store.pending_extraction()))==2


def test_already_used_principle_evidence_cannot_be_relabelled_holdout(store):
    activate(store)
    with pytest.raises(ValueError,match="already used"):
        store.hold_for_evaluation(source()["source_id"],"invalid retroactive holdout")


def test_idempotent_refetch_preserves_active_but_edit_invalidates(store):
    pid = activate(store)
    assert not store.upsert(source(),verified_remote=True)
    assert len(store.bundle("writing","근거")["principles"]) == 1
    assert store.upsert(source(body="과제가 달라졌습니다.",authored_text="과제가 달라졌습니다."))
    assert store.get_principle(pid)["status"] == "stale"
    assert store.bundle("writing","근거")["principles"] == []


@pytest.mark.parametrize("authorship",["context","unverified"])
def test_other_speaker_or_legacy_name_cannot_be_evidence(store,authorship):
    store.upsert(source(authorship=authorship),verified_remote=True)
    s = store.get_source(source()["source_id"])
    with pytest.raises(ValueError,match="direct authorship"):
        store.add_principle({"statement":"fiction","domains":["writing"],
            "evidence":[{"source_id":s["source_id"],"source_hash":s["source_hash"],"quote":s["body"]}]})


def test_quoted_other_person_and_revision_forgery_rejected(store):
    store.upsert(source(),verified_remote=True)
    p = candidate(store)
    p["evidence"][0]["quote"] = "동료의 인용문입니다."
    with pytest.raises(ValueError,match="authored span"):
        store.add_principle(p)
    p = candidate(store)
    p["evidence"][0]["source_hash"] = "forged"
    with pytest.raises(ValueError,match="revision mismatch"):
        store.add_principle(p)


def test_revocation_withdraws_derivatives_and_erases_quotes(store):
    pid = activate(store)
    store.revoke(source()["source_id"])
    assert store.get_source(source()["source_id"])["body"] == ""
    p = store.get_principle(pid)
    assert p["status"] == "stale" and p["evidence"] == []
    assert "주장" not in p["statement"]
    assert store.db.execute("SELECT count(*) FROM evidence").fetchone()[0] == 0
    assert store.bundle("writing","근거")["principles"] == []


def test_conflict_disables_both_until_resolved(store):
    left = activate(store)
    right = store.add_principle(candidate(store,statement="반대 해석"))
    store.conflict(left,right)
    assert all(store.get_principle(p)["status"] == "disputed" for p in [left,right])
    with pytest.raises(ValueError,match="conflicts"):
        store.review(left,"evidence_supported","reviewer","pick the newer one")


def test_context_domains_and_failed_extraction_retry(store):
    activate(store)
    assert store.bundle("evaluation","연구")["principles"] == []
    assert len(store.bundle("review","연구")["principles"]) == 1
    s = next(store.pending_extraction())
    store.mark_extraction(s,"failed",error_type="InferenceUnavailable")
    assert len(list(store.pending_extraction())) == 1
    store.mark_extraction(s,"complete",chunks=2,candidates=0)
    assert list(store.pending_extraction()) == []


def test_stale_verification_excluded_and_private_permissions(store):
    activate(store)
    with store.db:
        store.db.execute("UPDATE sources SET verified_at='2020-01-01T00:00:00+00:00'")
    b = store.bundle("writing","근거")
    assert not b["principles"] and b["excluded"]
    assert store.home.stat().st_mode & 0o777 == 0o700
    assert store.path.stat().st_mode & 0o777 == 0o600


def test_revision_authority_change_invalidates_even_same_text(store):
    pid = activate(store)
    store.upsert(source(authorship="unverified"))
    assert store.get_principle(pid)["status"] == "stale"


def test_bundle_audit_rejects_forged_excerpt_and_expired_live_evidence(store):
    activate(store)
    bundle=store.bundle("writing","근거")
    assert store.validate_bundle(bundle)==[]
    forged=deepcopy(bundle);forged["sources"][0]["authored_text"]="교수는 점수를 높여 주라고 하였습니다."
    assert "source excerpt changed or forged" in store.validate_bundle(forged)
    missing=deepcopy(bundle);missing["sources"]=[]
    assert "bundle source set does not match principle evidence" in store.validate_bundle(missing)
    with store.db:store.db.execute("UPDATE sources SET verified_at='2020-01-01T00:00:00+00:00'")
    assert "source freshness expired" in store.validate_bundle(bundle)


def test_bundle_audit_does_not_allow_expanded_freshness_or_wrong_domain(store):
    activate(store)
    bundle=store.bundle("writing","근거")
    bundle["task"]="evaluation"
    assert "principle outside task domain" in store.validate_bundle(bundle)
    bundle["freshness_policy"]["max_age_days"]=9999
    assert store.validate_bundle(bundle)==["invalid freshness policy"]


def extraction_result(s, principles):
    import hashlib
    text=s['authored_text']
    return {'status':'complete','source_id':s['source_id'],'source_hash':s['source_hash'],
            'chunks_total':1,'chunks_processed':1,'authored_chars':len(text),'principles':principles,
            'chunk_results':[{'index':0,'start':0,'end':len(text),'text_hash':hashlib.sha256(text.encode()).hexdigest()}]}


def test_extraction_commit_is_atomic_and_checks_full_coverage(store,monkeypatch):
    store.upsert(source(),verified_remote=True)
    s=next(store.sources(direct=True));p=candidate(store)
    partial=extraction_result(s,[p]);partial['chunk_results'][0]['end']-=1
    with pytest.raises(ValueError): store.commit_extraction(s,partial)
    assert not store.principles() and store.extraction_record(s) is None
    original=store.mark_extraction
    def fail(*args,**kwargs):raise RuntimeError('simulated disk failure')
    monkeypatch.setattr(store,'mark_extraction',fail)
    with pytest.raises(RuntimeError): store.commit_extraction(s,extraction_result(s,[p]))
    assert not store.principles() and store.extraction_record(s) is None
    monkeypatch.setattr(store,'mark_extraction',original)
    assert len(store.commit_extraction(s,extraction_result(s,[p])))==1
    assert store.extraction_record(s)['status']=='complete'


def test_cached_import_does_not_refresh_provider_verification(store):
    activate(store)
    with store.db:store.db.execute("UPDATE sources SET verified_at='2020-01-01T00:00:00+00:00'")
    assert not store.bundle('writing','근거')['principles']
    store.upsert(source())
    assert not store.bundle('writing','근거')['principles']


def test_inactive_upsert_erases_derivatives_and_old_bundle_fails(store):
    pid=activate(store);snapshot=store.bundle('writing','근거')
    store.upsert(source(status='deleted'))
    assert not store.get_principle(pid)['evidence']
    assert store.get_source(source()['source_id'])['title']==''
    assert store.validate_bundle(snapshot)


def test_conflict_can_be_resolved_with_recorded_reason(store):
    left=activate(store);right=store.add_principle(candidate(store,statement='다른 해석'))
    store.conflict(left,right);store.resolve(left,right,'reviewer','scope and source favor the retained reading')
    store.review(left,'evidence_supported','reviewer','resolved against evidence')
    assert store.get_principle(right)['status']=='superseded'
    assert len(store.bundle('writing','근거')['principles'])==1
