from commander_agent.core.semantic_recovery import _query_reuses_prior_evidence
from commander_agent.skills.result_semantics_recovery.scripts.capabilities import (
    CAPABILITY_SCHEMAS,
    CANDIDATE_ASSESSMENT_SCHEMA,
    normalise_candidate_assessment,
    normalise_capability_result,
    validate_normalised_capability_result,
)
from commander_agent.state.scope import build_scope_contract, detectable_scope_change


def _state():
    primary = (
        'index=sample sourcetype=audit service=alpha failure=* '
        '| where isnotnull(failure) '
        '| stats dc(failure) AS distinct_count BY actor'
    )
    check = (
        'index=sample sourcetype=audit service=alpha failure=* '
        '| where isnotnull(failure) '
        '| stats values(failure) AS values BY actor'
    )
    return {
        'case_id': 'synthetic-case',
        'evidence_version': 2,
        'evidence': [
            {
                'evidence_id': 'E1',
                'query': primary,
                'result_excerpt': '{"actor":"A","distinct_count":"1"}',
                'source': 'splunk',
                'method_class': 'aggregation_distinct',
                'evidence_version': 1,
            },
            {
                'evidence_id': 'E2',
                'query': check,
                'result_excerpt': '{"actor":"A","values":"x"}',
                'source': 'splunk',
                'method_class': 'aggregation_values',
                'evidence_version': 2,
            },
        ],
        'scope_contract': build_scope_contract(primary, check),
    }


def test_each_recovery_capability_has_its_own_tiny_schema():
    expected = {
        'semantic_field_review', 'schema_inspection', 'alternative_aggregation',
        'temporal_pattern_analysis', 'relationship_analysis', 'scope_expansion'
    }
    assert set(CAPABILITY_SCHEMAS) == expected
    # The old mega-schema fields must not be mandatory across every capability.
    assert 'recommended_spl' not in CAPABILITY_SCHEMAS['semantic_field_review']['required']
    assert 'candidate' not in CAPABILITY_SCHEMAS['schema_inspection']['properties']
    assert 'recommended_spl' not in CAPABILITY_SCHEMAS['temporal_pattern_analysis']['properties']
    assert 'candidate' in CANDIDATE_ASSESSMENT_SCHEMA['required']


def test_semantic_field_review_cannot_morph_into_a_query_action():
    state = _state()
    out = normalise_capability_result('semantic_field_review', {
        'verdict': 'INADEQUATE',
        'reason': 'The current representation is too coarse.',
        'support_ids': ['E1', 'E2'],
        'suggested_capabilities': ['schema_inspection'],
    }, state)
    assert out['decision'] == 'SEMANTIC_FINDING'
    assert out['recommended_spl'] is None
    assert out['observed_semantics']['field_verdict'] == 'INADEQUATE'
    assert out['suggested_capabilities'] == ['schema_inspection']


def test_schema_inspection_maps_only_to_schema_query():
    state = _state()
    out = normalise_capability_result('schema_inspection', {
        'action': 'RUN_QUERY',
        'reason': 'Inspect representative detail fields.',
        'recommended_spl': 'index=sample sourcetype=audit service=alpha failure=* | where isnotnull(failure) | table actor failure detail',
        'support_ids': ['E1'],
        'suggested_capabilities': [],
    }, state)
    assert out['decision'] == 'INVESTIGATE_SCHEMA'
    assert validate_normalised_capability_result('schema_inspection', out) == []


def test_temporal_capability_can_only_request_deterministic_analysis():
    state = _state()
    out = normalise_capability_result('temporal_pattern_analysis', {
        'action': 'RUN_ANALYSIS',
        'reason': 'Compare diversity growth over time.',
        'analysis_request': {
            'group_field': 'actor',
            'value_field': 'failure',
            'time_field': '_time',
            'measures': ['event_count', 'cumulative_distinct_count'],
            'comparison_feature': 'final_cumulative_distinct',
        },
        'support_ids': ['E1'],
        'suggested_capabilities': [],
    }, state)
    assert out['decision'] == 'RUN_TEMPORAL_ANALYSIS'
    assert out['recommended_spl'] is None
    assert validate_normalised_capability_result('temporal_pattern_analysis', out) == []


def test_scope_expansion_must_actually_change_search_scope():
    state = _state()
    same_scope = (
        'index=sample sourcetype=audit service=alpha failure=* '
        '| where isnotnull(failure) | stats count BY actor'
    )
    broader_scope = (
        'index=sample sourcetype=audit (service=alpha OR service=beta) failure=* '
        '| where isnotnull(failure) | stats count BY actor'
    )
    assert detectable_scope_change(same_scope, state['scope_contract']) is False
    assert detectable_scope_change(broader_scope, state['scope_contract']) is True


def test_recovery_query_equivalent_to_existing_evidence_is_detected_before_execution():
    state = _state()
    repeated = state['evidence'][1]['query'] + ' | sort actor'
    prior = _query_reuses_prior_evidence(state, repeated)
    assert prior is not None
    assert prior['evidence_id'] == 'E2'


def test_candidate_assessment_is_separate_and_answer_only_when_supported():
    state = _state()
    ambiguous = normalise_candidate_assessment({
        'status': 'AMBIGUOUS',
        'candidate': 'A',
        'support_ids': ['E1', 'E2'],
        'reason': 'Tie remains.',
        'requested_quantity': 'distinct failures',
        'confidence': 'medium',
    }, state)
    assert ambiguous['candidate'] is None

    supported = normalise_candidate_assessment({
        'status': 'SUPPORTED',
        'candidate': 'synthetic-actor',
        'support_ids': ['E1'],
        'reason': 'Machine evidence supports one candidate.',
        'requested_quantity': 'distinct failures',
        'confidence': 'high',
    }, state)
    assert supported['candidate'] == 'synthetic-actor'
    assert supported['support_ids'] == ['E1']
