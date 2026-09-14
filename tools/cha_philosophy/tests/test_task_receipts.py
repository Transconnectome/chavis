"""Original preparation, not rehashed replacement text, defines the task."""
from copy import deepcopy
import json

import pytest

from tools.cha_philosophy.store import Store
from tools.cha_philosophy.task import _task_source,prepare_task,audit_task_output
from tools.cha_philosophy.task_receipts import register_task_bundle,validate_task_receipt


@pytest.fixture
def prepared(tmp_path):
    store=Store(tmp_path/'private')
    bundle=prepare_task(store,'review','Review the whole original manuscript using this rubric.',[
        {'kind':'manuscript','content':'Original introduction. Methods use random assignment. Results contradict the abstract.'},
        {'kind':'rubric','content':'Methods 40%; Results 40%; Interpretation 20%. Apply all three criteria.'}])
    yield store,bundle
    store.close()


def output():
    return {'content':'Review complete.','applications':[],'claims':[],'uncertainties':[]}


def test_preparation_manifest_is_private_idempotent_and_contains_no_source_text(prepared):
    store,bundle=prepared
    path=store.home/'task_receipts'/(bundle['task_receipt_id']+'.json')
    assert path.stat().st_mode & 0o077==0
    assert path.parent.stat().st_mode & 0o077==0
    before=path.read_bytes()
    assert register_task_bundle(store,bundle)['task_receipt_id']==bundle['task_receipt_id']
    assert path.read_bytes()==before
    assert b'Original introduction' not in before and b'Methods 40%' not in before
    assert validate_task_receipt(store,bundle)==[]


def test_entire_rubric_omission_is_rejected_even_when_remaining_hashes_are_valid(prepared):
    store,original=prepared;changed=deepcopy(original)
    changed['sources']=[s for s in changed['sources'] if s.get('kind')!='rubric']
    result=audit_task_output(store,changed,output())
    assert result['status']=='invalid'
    assert 'task_input_manifest_changed' in result['errors']


def test_rehashed_partial_manuscript_cannot_replace_original_full_input(prepared):
    store,original=prepared;changed=deepcopy(original)
    changed['sources']=[_task_source('manuscript','Original introduction.','review')
                        if s.get('kind')=='manuscript' else s for s in changed['sources']]
    result=audit_task_output(store,changed,output())
    assert result['status']=='invalid'
    assert 'task_input_manifest_changed' in result['errors']


def test_unknown_or_missing_receipt_requires_preparation_again(prepared):
    store,original=prepared
    for receipt in [None,'f'*64,'../outside']:
        changed=deepcopy(original);changed['task_receipt_id']=receipt
        assert audit_task_output(store,changed,output())['status']=='invalid'


def test_receipt_is_not_semantic_or_reading_proof(prepared):
    store,bundle=prepared
    result=audit_task_output(store,bundle,output())
    assert result['status']=='structural_valid'
    assert result['semantic_review_required'] is True
    assert result['input_coverage_accounted'] is False
    assert result['whole_task_completion_verified'] is False


def test_receipt_publication_failure_leaves_no_partial_anchor(prepared,monkeypatch):
    store,original=prepared
    changed=deepcopy(original);changed['principles']=[]
    changed['sources'].append(_task_source('reference','Additional source.','review'))
    before=set((store.home/'task_receipts').iterdir())
    def fail(*a,**k):raise OSError('simulated interrupted publication')
    monkeypatch.setattr('tools.cha_philosophy.task_receipts.os.link',fail)
    with pytest.raises(OSError):register_task_bundle(store,changed)
    assert set((store.home/'task_receipts').iterdir())==before


def test_receipt_anchor_replaced_with_symlink_is_rejected(prepared,tmp_path):
    store,bundle=prepared
    path=store.home/'task_receipts'/(bundle['task_receipt_id']+'.json')
    outside=tmp_path/'outside.json';outside.write_bytes(path.read_bytes())
    path.unlink();path.symlink_to(outside)
    assert validate_task_receipt(store,bundle)==['task_receipt_unavailable']
