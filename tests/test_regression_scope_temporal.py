import asyncio
import re
from pathlib import Path

from commander_agent.core.executor import execute_splunk_search
from commander_agent.core.temporal_analysis import build_reduction_spl, normalise_analysis_request
from commander_agent.skills.registry import SKILLS
from commander_agent.skills.result_semantics_recovery.scripts.reviewer import validate_action_contract
from commander_agent.skills.temporal_pattern_analysis.scripts.analytics import analyse_rows
from commander_agent.state.scope import build_scope_contract, scope_violations, split_spl_pipeline


def test_pipeline_split_ignores_regex_pipes():
    query = (
        'index=x kind=y | where NOT match(lower(status), "^(ok|pass|none)$") '
        '| stats dc(code) AS n BY actor | sort 0 actor'
    )
    stages = split_spl_pipeline(query)
    assert len(stages) == 4
    assert 'ok|pass|none' in stages[1]


def test_scope_contract_blocks_silent_population_broadening():
    primary = (
        'index=training sourcetype=events service=identity status=* '
        '| where len(status)>0 | stats dc(status) AS n BY actor'
    )
    check = (
        'index=training sourcetype=events service=identity status=* '
        '| where len(status)>0 | stats values(status) AS values BY actor'
    )
    contract = build_scope_contract(primary, check)
    assert 'service=identity' in contract['base_scope_spl']
    good = contract['base_scope_spl'] + ' | stats count BY actor'
    bad = 'index=training sourcetype=events | stats count BY actor'
    assert scope_violations(good, contract) == []
    violations = scope_violations(bad, contract)
    assert any('service=identity' in item for item in violations)
    assert any('status=*' in item for item in violations)


def test_temporal_reduction_preserves_scope_and_lets_splunk_choose_bins():
    contract = build_scope_contract(
        'index=training sourcetype=events service=identity status=* | where len(status)>0 | stats dc(status) AS n BY actor'
    )
    request = normalise_analysis_request({
        'group_field': 'actor',
        'value_field': 'status',
        'time_field': '_time',
        'measures': ['event_count', 'distinct_count', 'cumulative_distinct_count', 'new_distinct_count'],
        'comparison_feature': 'final_cumulative_distinct',
    })
    query = build_reduction_spl(contract, request)
    assert query.startswith(contract['base_scope_spl'])
    assert '| bin _time | stats ' in query
    assert 'span=' not in query.lower()
    assert 'values(status) AS distinct_values' in query
    assert scope_violations(query, contract) == []


def test_temporal_analytics_are_data_driven_on_synthetic_series(tmp_path):
    rows = [
        {'_time': '2026-01-01T00:00:00Z', 'actor': 'A', 'event_count': '1', 'distinct_count': '1', 'distinct_values': '["x"]'},
        {'_time': '2026-01-01T00:01:00Z', 'actor': 'A', 'event_count': '3', 'distinct_count': '2', 'distinct_values': '["x", "y"]'},
        {'_time': '2026-01-01T00:02:00Z', 'actor': 'A', 'event_count': '5', 'distinct_count': '2', 'distinct_values': '["y", "z"]'},
        {'_time': '2026-01-01T00:00:00Z', 'actor': 'B', 'event_count': '2', 'distinct_count': '1', 'distinct_values': '["m"]'},
        {'_time': '2026-01-01T00:01:00Z', 'actor': 'B', 'event_count': '2', 'distinct_count': '1', 'distinct_values': '["m"]'},
        {'_time': '2026-01-01T00:02:00Z', 'actor': 'B', 'event_count': '2', 'distinct_count': '1', 'distinct_values': '["n"]'},
    ]
    request = {
        'group_field': 'actor',
        'value_field': 'status',
        'time_field': '_time',
        'measures': ['event_count', 'distinct_count', 'cumulative_distinct_count', 'new_distinct_count'],
        'comparison_feature': 'final_cumulative_distinct',
    }
    out = analyse_rows(rows, request)
    assert out['ok'] is True
    assert out['comparison']['leaders'] == ['A']
    features = {item['group']: item for item in out['groups']}
    assert features['A']['final_cumulative_distinct'] == 3.0
    assert features['B']['final_cumulative_distinct'] == 2.0
    assert features['A']['event_count']['slope_per_second'] > 0
    assert abs(features['B']['event_count']['slope_per_second']) < 1e-12


def test_semantic_action_contract_supports_temporal_without_spl():
    obj = {
        'decision': 'RUN_TEMPORAL_ANALYSIS',
        'capability': 'temporal_pattern_analysis',
        'candidate_status': 'ambiguous',
        'reason': 'Temporal diversity can distinguish the current hypotheses.',
        'candidate': None,
        'recommended_spl': None,
        'support_ids': ['E1'],
        'analysis_request': {
            'group_field': 'actor',
            'value_field': 'status',
            'time_field': '_time',
            'measures': ['event_count', 'cumulative_distinct_count'],
            'comparison_feature': 'final_cumulative_distinct',
        },
        'scope_change': {'requested': False, 'reason': None},
    }
    assert validate_action_contract(obj, current_candidate='') == []


def test_expand_scope_requires_explicit_justification():
    obj = {
        'decision': 'EXPAND_SCOPE',
        'capability': 'scope_expansion',
        'candidate_status': 'ambiguous',
        'reason': 'More population context requested.',
        'candidate': None,
        'recommended_spl': 'index=training | stats count',
        'support_ids': ['E1'],
        'scope_change': {'requested': False, 'reason': None},
    }
    errors = validate_action_contract(obj, current_candidate='')
    assert any('scope_change.requested=true' in x for x in errors)


class _NeverCalledSession:
    def __init__(self):
        self.called = False

    async def call_tool(self, *args, **kwargs):
        self.called = True
        raise AssertionError('scope guard should block before MCP execution')


def test_executor_scope_guard_blocks_silent_broad_query():
    contract = build_scope_contract(
        'index=training sourcetype=events service=identity | stats count BY actor'
    )
    case_state = {
        'case_id': 'synthetic',
        'evidence': [],
        'signature_to_evidence': {},
        'failed_query_shapes': set(),
        'scope_contract': contract,
    }
    metrics = {
        'search_attempts': 0,
        'queries': [],
        'guardrail_blocks': 0,
        'scope_guard_blocks': 0,
        'structured_output_rejections': 0,
        'search_calls': 0,
        'skill_contract_blocks': 0,
        'retry_diversity_blocks': 0,
        'current_run_cache_hits': 0,
        'query_cache_hits': 0,
        'tool_errors': 0,
        'zero_results': 0,
        'fatal_tool_errors': 0,
    }
    session = _NeverCalledSession()
    text, evidence, _ = asyncio.run(execute_splunk_search(
        session=session,
        tool_name='search_oneshot',
        arguments={'query': 'index=training sourcetype=events | stats count BY actor', 'output_format': 'json'},
        case_state=case_state,
        metrics=metrics,
        round_number=1,
        active_skills=[],
        phase='CHECK',
        question='synthetic question',
    ))
    assert evidence is None
    assert session.called is False
    assert 'scope invariant' in text.lower()


def test_current_registers_temporal_and_scope_skills_and_no_access_key_literals():
    assert 'temporal_pattern_analysis' in SKILLS
    assert 'scope_invariant_guard' in SKILLS
    roots = [Path('commander_agent'), Path('scripts'), Path('config')]
    texts = []
    for root in roots:
        for p in root.rglob('*'):
            if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc':
                texts.append(p.read_text(encoding='utf-8', errors='ignore'))
    combined = '\n'.join(texts)
    assert not re.search(r'\bAKIA[A-Z0-9]{16}\b', combined)


def test_live_preflight_adds_project_root_before_package_import():
    text = Path('scripts/live_splunk_preflight.py').read_text(encoding='utf-8')
    assert 'Path(__file__).resolve().parents[1]' in text
    assert 'sys.path.insert(0, str(PROJECT_ROOT))' in text
