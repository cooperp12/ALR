"""Mechanical candidate checks. Semantics must still be justified by the model."""
import json
import math
import re


def evidence_rows(record):
    payload = record.get('machine_result')
    if not isinstance(payload, dict):
        try:
            payload = json.loads(record.get('result_excerpt', ''))
        except (ValueError, TypeError):
            return [], False
    rows = payload.get('events', payload.get('results', []))
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        return [], False
    complete = not payload.get('rows_omitted', 0) and not payload.get('truncated', False)
    limit = record.get('result_limit')
    if limit is None or len(rows) >= limit:
        complete = False
    if re.search(r'\|\s*(head|tail|sample)\b', record.get('query', ''), re.I):
        complete = False
    return rows, complete


def candidate_errors(obj, case_state):
    if obj.get('status') != 'SUPPORTED':
        return []
    candidate = str(obj.get('candidate') or '').strip()
    if not candidate or candidate.upper() in {'SUPPORTED', 'AMBIGUOUS', 'CONFLICT', 'UNKNOWN', 'NONE'}:
        return ['SUPPORTED requires an actual candidate value, not a status label']
    known = {e.get('evidence_id'): e for e in case_state.get('evidence', [])
             if e.get('source') not in {'skill_reasoning', 'deterministic'}}
    ids = obj.get('support_ids') or []
    if not ids or any(i not in known for i in ids):
        return ['All support IDs must refer to existing machine evidence']
    rows = [r for i in ids for r in evidence_rows(known[i])[0]]
    if not any(candidate == str(v) for row in rows for v in row.values() if not isinstance(v, (dict,list))):
        return ['Candidate is absent from the cited structured evidence']
    question = str(case_state.get('question', '')).lower()
    ranking = any(w in question for w in ('most', 'highest', 'largest', 'maximum'))
    if not ranking:
        return []
    evidence_id = obj.get('ranking_evidence_id')
    group, metric = obj.get('group_field'), obj.get('metric_field')
    if evidence_id not in ids or not group or not metric:
        return ['Ranking support requires ranking_evidence_id, group_field and metric_field']
    record = known[evidence_id]
    rows, complete = evidence_rows(record)
    if not complete or not rows:
        return ['Ranking evidence is incomplete, capped or sampled; obtain a complete reduced result']
    if ('distinct' in question or 'unique' in question) and metric in {'failed_events','matching_events','count','event_count'}:
        return ['Total event frequency cannot support a distinct-value ranking']
    if ('distinct' in question or 'unique' in question) and not re.search(r'\bdc\([^)]*\)\s+AS\s+' + re.escape(metric) + r'\b', record.get('query', ''), re.I):
        return ['Selected metric is not an explicit distinct count in its evidence query']
    try:
        pairs = [(str(r[group]), float(r[metric])) for r in rows]
        if any(not math.isfinite(v) or v < 0 for _,v in pairs):
            raise ValueError()
        if len({k for k,v in pairs}) != len(pairs):
            raise ValueError()
    except (KeyError, ValueError, TypeError):
        return ['Invalid numeric ranking or duplicate groups']
    if any('population_keys' in r and str(r['population_keys']) != str(len(rows)) for r in rows):
        return ['Returned ranking does not cover its declared population']
    top = max(v for _, v in pairs)
    winners = [k for k,v in pairs if v == top]
    if winners != [candidate]:
        return ['Selected metric has a tie or the candidate is not its unique maximum']
    if not obj.get('metric_justification'):
        return ['Explain why the selected metric answers the question, independently of which candidate wins']
    return []
