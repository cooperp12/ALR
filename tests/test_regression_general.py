import asyncio
import json
import runpy
import sys
from pathlib import Path
from copy import deepcopy
from types import SimpleNamespace

import pytest
from commander_agent.state.candidate_checks import candidate_errors, evidence_rows
from commander_agent.state.orchestration import (record_capability_attempt, available_recovery_capabilities,
    record_supervisor_evaluation, ensure_orchestration_state, supervisor_context)
from commander_agent.reasoning import local_ollama
from commander_agent.core.reference_investigation import reference_queries, verify_assessment
from commander_agent.skills.strategy import compose_query_strategy
from commander_agent.state.scope import build_scope_contract, scope_violations
from commander_agent.skills.result_semantics_recovery.scripts.capabilities import normalise_capability_result, validate_normalised_capability_result


def state():
    rows = [{'actor':'A','code_count':1,'message_count':3}, {'actor':'B','code_count':1,'message_count':2}, {'actor':'C','code_count':1,'message_count':2}]
    return {'question':'Which actor has the most distinct errors?', 'evidence_version':1,
            'evidence':[{'evidence_id':'E1','source':'splunk','query':'index=synthetic | stats dc(code) AS code_count dc(message) AS message_count BY actor',
                         'machine_result':{'events':rows}, 'result_limit':50}], 'scope_contract':{}}


def assessment(metric='message_count', candidate='A'):
    return {'status':'SUPPORTED','candidate':candidate,'support_ids':['E1'],
            'ranking_evidence_id':'E1','group_field':'actor','metric_field':metric,
            'metric_justification':'Distinct failure descriptions distinguish the observed operations.'}


@pytest.mark.parametrize('candidate', ['SUPPORTED','UNKNOWN','not-in-data'])
def test_reject_status_and_absent_candidates(candidate):
    assert candidate_errors(assessment(candidate=candidate), state())


def test_tie_rejected_unique_maximum_accepted():
    assert 'tie' in candidate_errors(assessment('code_count'), state())[0]
    assert candidate_errors(assessment(), state()) == []
    assert candidate_errors(assessment(candidate='B'), state())


@pytest.mark.parametrize('mutation', ['omitted','cap','head','invalid_number','duplicate','missing_support','population'])
def test_reject_incomplete_or_inconsistent_rankings(mutation):
    s=state(); a=assessment(); e=s['evidence'][0]
    if mutation=='omitted': e['machine_result']['rows_omitted']=1
    if mutation=='cap': e['result_limit']=3
    if mutation=='head': e['query'] += ' | head 1'
    if mutation=='invalid_number': e['machine_result']['events'][0]['message_count']='NaN'
    if mutation=='duplicate': e['machine_result']['events'][1]['actor']='A'
    if mutation=='missing_support': a['support_ids']=['E404']
    if mutation=='population': e['machine_result']['events'][0]['population_keys']='99'
    assert candidate_errors(a,s)


def test_frequency_cannot_break_distinct_tie():
    s=state();s['evidence'][0]['machine_result']['events'][0]['failed_events']=500
    assert candidate_errors(assessment('failed_events'),s)


def test_rephrasing_gap_does_not_reopen_failed_route():
    s=state();record_capability_attempt(s,'schema_inspection','no_new_evidence')
    for i in range(5):
        record_supervisor_evaluation(s,{'remaining_gap':('verbose '+str(i)+' ')*2000})
    assert 'schema_inspection' not in available_recovery_capabilities(s)
    assert len(supervisor_context(s)['remaining_gap']) <= 360


def test_specific_prerequisite_reopens_but_unrelated_progress_does_not():
    s=state();record_capability_attempt(s,'alternative_aggregation','missing_prerequisite',prerequisites=['schema_inspection'])
    assert 'alternative_aggregation' not in available_recovery_capabilities(s)
    s['evidence_version']+=1
    record_capability_attempt(s,'relationship_analysis','progress')
    assert 'alternative_aggregation' not in available_recovery_capabilities(s)
    record_capability_attempt(s,'schema_inspection','progress')
    assert 'alternative_aggregation' in available_recovery_capabilities(s)


def test_unexplained_defer_rejected():
    out=normalise_capability_result('schema_inspection', {'action':'DEFER'},state())
    assert validate_normalised_capability_result('schema_inspection',out)


def test_semantic_review_is_analytical_not_new_observational_evidence():
    s=state();before=s['evidence_version']
    out=normalise_capability_result('semantic_field_review',{'verdict':'INADEQUATE','reason':'Codes are coarse.'},s)
    assert out['decision']=='SEMANTIC_FINDING'
    record_capability_attempt(s,'semantic_field_review','analytical_progress')
    assert s['evidence_version']==before
    assert 'semantic_field_review' not in available_recovery_capabilities(s)


def test_reference_queries_preserve_scope_and_compare_metrics():
    strategy=compose_query_strategy('What IAM user access key has the most distinct errors?', ['spl_distinct_count','aws_cloudtrail'])
    scope=build_scope_contract(strategy['primary_query'],strategy['check_query'])
    queries=reference_queries(strategy,scope)
    assert len(queries)==2
    for _,q in queries:
        assert scope_violations(q,scope)==[]
    assert 'dc(errorMessage)' in queries[1][1]
    assert 'dc(operation_error)' in queries[1][1]
    assert 'head 1' not in queries[1][1]


def test_no_reference_for_unrelated_question():
    assert reference_queries({'resolver_config':{'distinct_field':'eventName'}},{})==[]


@pytest.mark.parametrize('counts, expected', [([3,2,2],True),([4,2,2],False)])
def test_separate_grouped_pair_calculation_must_agree(monkeypatch,counts,expected):
    import commander_agent.core.reference_investigation as module
    s=state();seen=[]
    async def fake(**kwargs):
        seen.append(kwargs)
        e={'evidence_id':'E2','source':'splunk','query':kwargs['arguments']['query'],'result_limit':50,
           'machine_result':{'events':[{'actor':k,'checked_count':v} for k,v in zip('ABC',counts)]}}
        s['evidence'].append(e)
        return '',e,{}
    monkeypatch.setattr(module,'execute_splunk_search',fake)
    result=asyncio.run(verify_assessment(None,assessment(),s['question'],s,{},[]))
    assert result.get('calculation_verified',False)==expected
    assert 'stats count BY actor message' in seen[0]['arguments']['query']
    if not expected: assert result['candidate'] is None


def test_empty_output_without_limit_is_not_claimed_truncation():
    response={'message':{'content':'','thinking':'short'},'eval_count':10,'done_reason':'stop'}
    assert not local_ollama.structured_response_looks_truncated(response,900)


def test_supported_retry_controls(monkeypatch):
    monkeypatch.setattr(local_ollama,'thinking_controls',lambda model: ('low','medium','high'))
    assert local_ollama.supported_think('model',False,True)=='low'
    monkeypatch.setattr(local_ollama,'thinking_controls',lambda model: (True,False))
    assert local_ollama.supported_think('model',True,True) is False


def test_context_reserves_response_and_rejects_oversize_before_call(monkeypatch):
    def forbidden(**kwargs): raise AssertionError('must not call model')
    monkeypatch.setattr(local_ollama,'_ollama_chat',forbidden)
    with pytest.raises(ValueError,match='output reserve'):
        local_ollama.structured_chat(role='supervisor',messages=[{'role':'user','content':'x'*40000}],schema={})


def test_direct_preflight_script_imports_from_foreign_directory(tmp_path,monkeypatch):
    script=Path('scripts/ollama_role_preflight.py').resolve()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys,'path',[p for p in sys.path if 'stale_build' not in p and p!=''])
    data=runpy.run_path(str(script),run_name='test_preflight_import')
    assert callable(data['main'])
