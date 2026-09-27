"""Bounded, answer-free CloudTrail measurement comparison inside existing scope."""
from commander_agent.core.executor import execute_splunk_search
from commander_agent.state.orchestration import record_capability_attempt
from commander_agent.state.evidence import current_evidence_version
from commander_agent.state.release import record_candidate_verification


def reference_queries(strategy, scope):
    config = strategy.get('resolver_config', {})
    if config.get('distinct_field') != 'errorCode':
        return []
    base = scope.get('base_scope_spl', '')
    group = config.get('group_field', '')
    if not base or group not in {'userIdentity.accessKeyId', 'userIdentity.userName'} or 'aws:cloudtrail' not in base:
        return []
    # Retain all original filters. A separate reviewed scope change is needed to audit
    # error-message-only records excluded by an inherited errorCode=* predicate.
    extraction = (' | spath input=_raw path=errorCode output=raw_code'
                  ' | spath input=_raw path=errorMessage output=raw_message'
                  ' | spath input=_raw path=userIdentity.type output=raw_identity_type')
    inspect = (base + extraction + f' | table {group} userIdentity.type eventName errorCode errorMessage raw_code raw_message raw_identity_type eventID'
               '| head 12')
    # JSON tuples avoid ambiguous delimiter concatenation. Messages retain exact wording;
    # normalising resource IDs without evidence could erase meaningful distinctions.
    prepare = (base + ' | eval operation_error=json_object("operation",eventName,"code",errorCode,"message",errorMessage)')
    compare = (prepare + f' | stats dc(errorCode) AS code_count dc(errorMessage) AS message_count '
               f'dc(operation_error) AS operation_error_count count AS failed_events '
               f'count(errorMessage) AS events_with_message BY {group}'
               ' | eventstats count AS population_keys | sort 0 ' + group)
    return [('schema_inspection', inspect), ('alternative_aggregation', compare)]


async def run_reference_investigation(session, question, strategy, case_state, metrics, active_skills):
    if case_state.get('reference_investigation_attempted'):
        return
    queries = reference_queries(strategy, case_state.get('scope_contract') or {})
    if not queries:
        return
    case_state['reference_investigation_attempted'] = True
    for capability, query in queries:
        before = current_evidence_version(case_state)
        result, evidence, _ = await execute_splunk_search(
            session=session, tool_name='search_oneshot', arguments={
                'query': query, 'earliest_time': '0', 'latest_time': 'now',
                'max_count': 50, 'output_format': 'json'},
            case_state=case_state, metrics=metrics, round_number=0,
            active_skills=active_skills, phase='CHECK', question=question)
        metrics['reference_queries'] = metrics.get('reference_queries', 0) + 1
        print('\nREFERENCE INVESTIGATION — ' + capability + '\n' + result)
        if evidence:
            # Establish a prerequisite without exhausting the LLM capability itself.
            record_capability_attempt(case_state, 'reference_' + capability, 'progress',
                                      before, current_evidence_version(case_state))
        if case_state.get('fatal_tool_error'):
            break


def grouped_check_query(record, group, metric):
    import re
    from commander_agent.state.scope import split_spl_pipeline
    if not re.fullmatch(r'[A-Za-z_][\w.]*', group or ''):
        return None
    stages = split_spl_pipeline(record.get('query', ''))
    for index, stage in enumerate(stages):
        if re.match(r'^stats\b', stage, re.I):
            match = re.search(r'\bdc\(\s*([\w.]+)\s*\)\s+AS\s+' + re.escape(metric) + r'\b', stage, re.I)
            if not match:
                return None
            field = match.group(1)
            return (' | '.join(stages[:index]) + f' | stats count BY {group} {field}'
                    f' | stats count AS checked_count BY {group} | eventstats count AS population_keys | sort 0 {group}')
    return None


async def verify_assessment(session, assessment, question, state, metrics, skills):
    from commander_agent.state.candidate_checks import evidence_rows
    if not assessment or assessment.get('status') != 'SUPPORTED' or not assessment.get('ranking_evidence_id'):
        return assessment
    source = next((e for e in state['evidence'] if e['evidence_id'] == assessment['ranking_evidence_id']), None)
    group, metric = assessment.get('group_field'), assessment.get('metric_field')
    query = grouped_check_query(source or {}, group, metric)
    if not query:
        return {**assessment, 'status': 'AMBIGUOUS', 'candidate': None, 'reason': 'Selected ranking has no supported grouped-pair cross-check.'}
    _, check, _ = await execute_splunk_search(session=session, tool_name='search_oneshot',
        arguments={'query': query, 'max_count': 50, 'output_format': 'json', 'earliest_time': '0', 'latest_time': 'now'},
        case_state=state, metrics=metrics, round_number=0, active_skills=skills, phase='CHECK', question=question)
    expected, full = evidence_rows(source)
    actual, checked = evidence_rows(check or {})
    try:
        a = {str(r[group]): float(r[metric]) for r in expected}
        b = {str(r[group]): float(r['checked_count']) for r in actual}
        # A group with no non-null values is omitted by grouped-pair SPL and has dc=0.
        agrees = bool(a) and full and checked and all(a.get(k, 0) == b.get(k, 0) for k in set(a) | set(b))
    except (KeyError, TypeError, ValueError):
        agrees = False
    if not agrees:
        metrics['ranking_check_failures'] = metrics.get('ranking_check_failures', 0) + 1
        return {**assessment, 'status': 'AMBIGUOUS', 'candidate': None, 'reason': 'Grouped-pair calculation was incomplete or disagreed with the selected ranking.'}
    metrics['ranking_checks_passed'] = metrics.get('ranking_checks_passed', 0) + 1
    verified = {**assessment, 'support_ids': list(dict.fromkeys(assessment['support_ids'] + [check['evidence_id']])),
                'calculation_verified': True, 'verification_kind': 'same-source independent calculation'}
    record_candidate_verification(state, verified, check.get('evidence_id'))
    return verified
