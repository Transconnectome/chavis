"""A real offline CLI round trip preserves full input and rejects missing work."""
import json
import pytest

from tools.cha_philosophy.cli import main
from tools.cha_philosophy.tests.test_task_coverage import declared_coverage


def test_full_prepare_plan_read_and_audit_cli_roundtrip(tmp_path,capsys):
    home=tmp_path/'private'
    request=tmp_path/'request.txt';request.write_text('Review all sections against the supplied rubric.')
    manuscript=tmp_path/'manuscript.txt'
    original='Methods and results require joint examination. 연구 결과.\r\nMixed CR\rFinal🙂\n'*600
    manuscript.write_bytes(original.encode('utf-8'))
    rubric=tmp_path/'rubric.txt';rubric.write_text('Methods 50%; Results 50%. Missing data is unknown, not zero.')
    bundle_path=tmp_path/'bundle.json';plan_path=tmp_path/'plan.json'
    def call(args):
        code=main(['--home',str(home),*map(str,args)])
        return code,json.loads(capsys.readouterr().out)
    code,result=call(['prepare','review','--request',request,'--input',f'manuscript={manuscript}',
                      '--input',f'rubric={rubric}','--output',bundle_path])
    assert code==0 and result['output_path']==str(bundle_path)
    bundle=json.loads(bundle_path.read_text())
    assert next(s['authored_text'] for s in bundle['sources'] if s.get('kind')=='manuscript')==original
    assert bundle['preparation']['local_context_fit'] is False
    code,_=call(['plan-task',bundle_path,'--output',plan_path]);assert code==0
    plan=json.loads(plan_path.read_text());assert 'coverage_schema' in plan
    parts={s['source_id']:[] for s in bundle['sources'] if s['platform']=='task'}
    for unit in plan['units']:
        code,result=call(['read-task-unit',bundle_path,unit['unit_id']])
        assert code==0 and result['source_hash']==unit['source_hash']
        parts[result['source_id']].append(result['content'])
    for src in bundle['sources']:
        assert ''.join(parts[src['source_id']])==src['authored_text']
    coverage_path=tmp_path/'coverage.json';coverage=declared_coverage(plan)
    coverage_path.write_text(json.dumps(coverage))
    output_path=tmp_path/'output.json';output_path.write_text(json.dumps({
        'content':'Mechanical integration fixture; no semantic review was performed.',
        'applications':[],'claims':[],'uncertainties':['No completed review is asserted.']}))
    code,result=call(['audit-task',output_path,bundle_path,'--coverage',coverage_path])
    assert code==0 and result['input_coverage_accounted'] is True
    assert result['whole_task_completion_verified'] is False
    assert result['input_reading_verified'] is False
    assert result['semantic_review_required'] is True
    coverage['unit_notes'].pop();coverage_path.write_text(json.dumps(coverage))
    code,result=call(['audit-task',output_path,bundle_path,'--coverage',coverage_path])
    assert code==1 and result['status']=='invalid'
    assert result['input_coverage_accounted'] is False
    bundle['sources']=[s for s in bundle['sources'] if s.get('kind')!='rubric']
    tampered=tmp_path/'tampered.json';tampered.write_text(json.dumps(bundle))
    code,result=call(['plan-task',tampered])
    assert code==1 and result['error_code']=='task_input_manifest_changed'


@pytest.mark.parametrize('target',['bundle','output','coverage'])
@pytest.mark.parametrize('invalid',['duplicate','null','nan'])
def test_cli_rejects_ambiguous_or_invalid_task_json(tmp_path,capsys,target,invalid):
    from tools.cha_philosophy.store import Store
    from tools.cha_philosophy.task import prepare_task
    from tools.cha_philosophy.task_coverage import plan_task_coverage
    home=tmp_path/'private';store=Store(home)
    bundle=prepare_task(store,'review','Review the entire manuscript.',[
        {'kind':'manuscript','content':'Methods and results.'}])
    store.close()
    values={'bundle':bundle,'coverage':declared_coverage(plan_task_coverage(bundle)),
            'output':{'content':'Mechanical fixture.','applications':[],'claims':[],
                      'uncertainties':['No semantic review.']}}
    paths={key:tmp_path/(key+'.json') for key in values}
    for key,value in values.items():paths[key].write_text(json.dumps(value))
    if invalid=='duplicate':
        key={'bundle':'sources','output':'content','coverage':'unit_notes'}[target]
        text='{'+json.dumps(key)+':null,'+json.dumps(values[target])[1:]
    elif invalid=='null':text='null'
    else:text=json.dumps(values[target])[:-1]+',"ambiguous":NaN}'
    paths[target].write_text(text)
    code=main(['--home',str(home),'audit-task',str(paths['output']),str(paths['bundle']),
               '--coverage',str(paths['coverage'])])
    result=json.loads(capsys.readouterr().out)
    assert code==1 and result['status'] in {'invalid','error'}


def test_api_audit_rejects_invalid_unicode_and_explicit_null_coverage(tmp_path):
    from copy import deepcopy
    from tools.cha_philosophy.store import Store
    from tools.cha_philosophy.task import prepare_task,audit_task_output
    store=Store(tmp_path/'private')
    try:
        bundle=prepare_task(store,'review','Review.',[])
        output={'content':'Fixture.','applications':[],'claims':[],'uncertainties':[]}
        assert audit_task_output(store,bundle,output,coverage=None)['status']=='invalid'
        broken=deepcopy(bundle);broken['sources'][0]['authored_text']='\ud800'
        assert audit_task_output(store,broken,output)['errors']==['task_source_integrity_failed']
        output['content']='\ud800'
        assert audit_task_output(store,bundle,output)['errors']==['invalid_json_response']
    finally:store.close()
