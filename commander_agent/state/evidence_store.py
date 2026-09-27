"""Durable evidence/observation state for ALR legacy compatibility.

Tools produce machine evidence. Python preserves that evidence, extracts immutable
observations, assigns stable IDs to complete measurements, freezes model-approved
measurement semantics as contracts, and derives winners/ties mechanically.
"""
from __future__ import annotations

import json, math, re, sqlite3
from datetime import datetime
from pathlib import Path

from commander_agent.config import (
    CASE_STATE_DB_FILE, OBSERVATION_LEDGER_FILE,
    MEASUREMENT_CONTRACT_LEDGER_FILE, CANDIDATE_DERIVATION_LEDGER_FILE,
)
from commander_agent.state.io import append_jsonl, stable_hash


def _now(): return datetime.now().isoformat()
def _j(v): return json.dumps(v, ensure_ascii=False, sort_keys=True)
def _num(v):
    if isinstance(v, bool): return None
    try: x=float(v)
    except (TypeError,ValueError): return None
    if not math.isfinite(x): return None
    return int(x) if x.is_integer() else x


def _connect():
    p=Path(CASE_STATE_DB_FILE); p.parent.mkdir(parents=True, exist_ok=True)
    db=sqlite3.connect(p); db.execute("PRAGMA journal_mode=WAL"); db.execute("PRAGMA synchronous=NORMAL")
    db.execute("""CREATE TABLE IF NOT EXISTS observations(
      case_id TEXT, observation_id TEXT, created TEXT, epistemic_state TEXT, kind TEXT,
      evidence_id TEXT, subject_value TEXT, field TEXT, operator TEXT, value_json TEXT,
      group_field TEXT, group_value TEXT, metric_field TEXT, source_expression TEXT,
      complete INTEGER, fingerprint TEXT, PRIMARY KEY(case_id,observation_id), UNIQUE(case_id,fingerprint))""")
    db.execute("""CREATE TABLE IF NOT EXISTS measurement_contracts(
      case_id TEXT, contract_id TEXT, created TEXT, requested_quantity TEXT, group_field TEXT,
      metric_field TEXT, source_expression TEXT, evidence_id TEXT, status TEXT,
      support_ids_json TEXT, reason TEXT, confidence TEXT, fingerprint TEXT,
      PRIMARY KEY(case_id,contract_id), UNIQUE(case_id,fingerprint))""")
    db.execute("""CREATE TABLE IF NOT EXISTS candidate_derivations(
      case_id TEXT, candidate_id TEXT, created TEXT, contract_id TEXT, status TEXT,
      winners_json TEXT, maximum_value REAL, evidence_id TEXT, support_ids_json TEXT,
      fingerprint TEXT, PRIMARY KEY(case_id,candidate_id), UNIQUE(case_id,fingerprint))""")
    db.commit(); return db


def ensure_store_state(case_state):
    case_state.setdefault("observations", [])
    case_state.setdefault("observation_fingerprints", set())
    case_state.setdefault("measurement_candidates", [])
    case_state.setdefault("measurement_candidate_fingerprints", set())
    case_state.setdefault("measurement_contracts", [])
    case_state.setdefault("active_measurement_contract", None)
    case_state.setdefault("candidate_derivations", [])
    case_state.setdefault("evidence_conflicts", [])
    return case_state


def _stats_stage(query):
    return next((s.strip() for s in str(query or '').split('|') if re.match(r'\s*stats\b',s,re.I)), '')


def _group_fields(query):
    m=re.search(r'\bBY\s+(.+)$', _stats_stage(query), re.I)
    if not m: return []
    return [t for t in re.split(r'[\s,]+',m.group(1).strip()) if re.fullmatch(r'[A-Za-z_][\w.]*',t or '')]


def distinct_metric_mappings(query):
    return [
      {"operator":"distinct_count","source_expression":expr.strip(),"metric_field":alias.strip()}
      for expr,alias in re.findall(r'\bdc\(\s*([^()]+?)\s*\)\s+AS\s+([A-Za-z_][\w.]*)',_stats_stage(query),re.I)
    ]


def _rows(payload):
    r=(payload or {}).get('events',(payload or {}).get('results',[])) if isinstance(payload,dict) else []
    return [x for x in r if isinstance(x,dict)] if isinstance(r,list) else []


def _completeness(evidence,payload,rows):
    checks={
        'machine_rows': bool(rows),
        'rows_not_omitted': not bool((payload or {}).get('rows_omitted',0)),
        'not_truncated': not bool((payload or {}).get('truncated',False)),
        'not_sampled_or_limited_in_spl': not bool(re.search(r'\|\s*(head|tail|sample)\b',str((evidence or {}).get('query') or ''),re.I)),
        'below_transport_limit': True,
    }
    lim=(evidence or {}).get('result_limit')
    try:
        if lim is not None and len(rows)>=int(lim): checks['below_transport_limit']=False
    except (TypeError,ValueError):
        checks['below_transport_limit']=False
    return all(checks.values()), checks


def _complete(evidence,payload,rows):
    return _completeness(evidence,payload,rows)[0]


def _add_obs(case_state, obs):
    ensure_store_state(case_state)
    core={k:obs.get(k) for k in ('kind','evidence_id','subject_value','field','operator','value','group_field','group_value','metric_field','source_expression','complete')}
    fp=stable_hash({'case_id':case_state.get('case_id'),**core})
    if fp in case_state['observation_fingerprints']: return None
    item={'record_type':'observation','case_id':case_state.get('case_id'),'observation_id':'O-'+fp[:12],
          'created':_now(),'epistemic_state':'OBSERVED',**core,'fingerprint':fp}
    case_state['observation_fingerprints'].add(fp); case_state['observations'].append(item); append_jsonl(OBSERVATION_LEDGER_FILE,item)
    try:
        with _connect() as db:
            db.execute("INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
             (item['case_id'],item['observation_id'],item['created'],item['epistemic_state'],item['kind'],item['evidence_id'],item.get('subject_value'),item.get('field'),item.get('operator'),_j(item.get('value')),item.get('group_field'),item.get('group_value'),item.get('metric_field'),item.get('source_expression'),1 if item.get('complete') else 0,fp))
    except sqlite3.Error: pass
    return item


def _measurement_id(evidence_id, candidate_fingerprint):
    safe=re.sub(r'[^A-Za-z0-9_.-]+','_',str(evidence_id or 'EVIDENCE')).strip('_') or 'EVIDENCE'
    return f"MEAS-{safe}-{candidate_fingerprint[:8]}"


def ingest_machine_evidence(case_state,evidence,payload):
    """Promote validated machine rows into observations and complete measurements."""
    ensure_store_state(case_state); rows=_rows(payload)
    if not evidence or not rows: return []
    query=str(evidence.get('query') or ''); groups=_group_fields(query); mappings=distinct_metric_mappings(query)
    complete, completeness_checks=_completeness(evidence,payload,rows)
    eid=str(evidence.get('evidence_id') or ''); out=[]
    primary=next((g for g in groups if all(g in r for r in rows)), groups[0] if groups else None)
    for row in rows:
        subj=str(row.get(primary)) if primary and row.get(primary) is not None else None
        for field,value in row.items():
            if not isinstance(value,(str,int,float,bool,type(None))): continue
            o=_add_obs(case_state,{'kind':'field_value','evidence_id':eid,'subject_value':subj,'field':str(field),'operator':'reported_value','value':value,'group_field':primary,'group_value':subj,'metric_field':None,'source_expression':str(field),'complete':complete})
            if o: out.append(o)
        for m in mappings:
            if m['metric_field'] not in row: continue
            n=_num(row.get(m['metric_field']))
            if n is None: continue
            o=_add_obs(case_state,{'kind':'measurement','evidence_id':eid,'subject_value':subj,'field':m['source_expression'],'operator':'distinct_count','value':n,'group_field':primary,'group_value':subj,'metric_field':m['metric_field'],'source_expression':m['source_expression'],'complete':complete})
            if o: out.append(o)
    if complete and primary:
        for m in mappings:
            vals={}; ok=True
            for row in rows:
                n=_num(row.get(m['metric_field'])); k=row.get(primary)
                if n is None or k is None: ok=False; break
                vals[str(k)]=n
            if ok and len(vals)==len(rows):
                core={'evidence_id':eid,'group_field':primary,'operator':'distinct_count','metric_field':m['metric_field'],'source_expression':m['source_expression'],'values':vals,'complete':True}
                fp=stable_hash(core)
                obs_ids=sorted(o['observation_id'] for o in case_state['observations']
                    if o.get('kind')=='measurement' and o.get('evidence_id')==eid
                    and o.get('group_field')==primary and o.get('metric_field')==m['metric_field']
                    and o.get('source_expression')==m['source_expression'] and o.get('complete'))
                candidate={
                    **core,
                    'measurement_id':_measurement_id(eid,fp),
                    'observation_ids':obs_ids,
                    'support_ids':[eid],
                    'completeness_checks':dict(completeness_checks),
                    'fingerprint':fp,
                }
                if fp not in case_state['measurement_candidate_fingerprints']:
                    case_state['measurement_candidate_fingerprints'].add(fp); case_state['measurement_candidates'].append(candidate)
    return out


def measurement_candidates(case_state):
    ensure_store_state(case_state); return [dict(x) for x in case_state['measurement_candidates'] if x.get('complete')]


def measurement_by_id(case_state, measurement_id):
    mid=str(measurement_id or '').strip()
    return next((dict(c) for c in measurement_candidates(case_state) if c.get('measurement_id')==mid),None)


def active_measurement_contract(case_state):
    ensure_store_state(case_state); c=case_state.get('active_measurement_contract'); return dict(c) if isinstance(c,dict) else None


def create_measurement_contract(case_state,*,requested_quantity,measurement_id=None,evidence_id=None,group_field=None,metric_field=None,source_expression=None,support_ids=None,reason='',confidence='medium',status='SEMANTICALLY_SUPPORTED'):
    """Freeze one existing complete measurement.

    ALR legacy compatibility's normal path supplies only ``measurement_id``. Legacy field arguments are
    accepted for tests/backwards compatibility, but provenance and field mappings are
    always reconstructed from the selected machine measurement by Python.
    """
    ensure_store_state(case_state)
    selected=measurement_by_id(case_state,measurement_id) if measurement_id else next((c for c in measurement_candidates(case_state)
        if c['evidence_id']==evidence_id and c['group_field']==group_field and c['metric_field']==metric_field and c['source_expression']==source_expression),None)
    if not selected: raise ValueError('Measurement contract must select an existing complete machine measurement')
    machine_support=list(dict.fromkeys(selected.get('support_ids') or [selected['evidence_id']]))
    core={
        'requested_quantity':str(requested_quantity or '').strip(),
        'measurement_id':selected['measurement_id'],
        'group_field':selected['group_field'],
        'metric_field':selected['metric_field'],
        'source_expression':selected['source_expression'],
        'evidence_id':selected['evidence_id'],
        'status':status,
        'support_ids':machine_support,
        'support_observation_ids':list(selected.get('observation_ids') or []),
        'reason':str(reason or '').strip(),
        'confidence':confidence if confidence in {'high','medium','low'} else 'medium',
    }
    fp=stable_hash({'case_id':case_state.get('case_id'),**core}); existing=next((x for x in case_state['measurement_contracts'] if x.get('fingerprint')==fp),None)
    if existing: case_state['active_measurement_contract']=existing; return existing
    item={'record_type':'measurement_contract','case_id':case_state.get('case_id'),'contract_id':'M-'+fp[:12],'created':_now(),**core,
          'fingerprint':fp,'values':dict(selected['values']),'provenance':{
              'measurement_id':selected['measurement_id'],'evidence_id':selected['evidence_id'],
              'observation_ids':list(selected.get('observation_ids') or []),
              'completeness_checks':dict(selected.get('completeness_checks') or {}),
          }}
    case_state['measurement_contracts'].append(item); case_state['active_measurement_contract']=item; append_jsonl(MEASUREMENT_CONTRACT_LEDGER_FILE,item)
    try:
        with _connect() as db:
            # Existing ALR legacy compatibility SQLite schema is retained; measurement_id/provenance live in JSON state.
            db.execute("INSERT OR IGNORE INTO measurement_contracts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(item['case_id'],item['contract_id'],item['created'],item['requested_quantity'],item['group_field'],item['metric_field'],item['source_expression'],item['evidence_id'],item['status'],_j(item['support_ids']),item['reason'],item['confidence'],fp))
    except sqlite3.Error: pass
    return item



def invalidate_active_measurement_contract(case_state, reason=""):
    ensure_store_state(case_state)
    c=case_state.get("active_measurement_contract")
    if not isinstance(c,dict): return None
    c["status"]="INVALIDATED"
    c["invalidated_reason"]=str(reason or "")
    case_state["active_measurement_contract"]=None
    return c

def derive_candidate_from_contract(case_state,contract=None):
    ensure_store_state(case_state); contract=contract or active_measurement_contract(case_state)
    if not contract: return None
    vals={str(k):_num(v) for k,v in dict(contract.get('values') or {}).items()}
    if not vals or any(v is None for v in vals.values()): return None
    top=max(vals.values()); winners=sorted(k for k,v in vals.items() if v==top); status='UNIQUE_MAXIMUM' if len(winners)==1 else 'TIED_MAXIMUM'
    core={'contract_id':contract['contract_id'],'status':status,'winners':winners,'maximum_value':top,'evidence_id':contract['evidence_id'],'support_ids':list(contract.get('support_ids') or [contract['evidence_id']])}
    fp=stable_hash({'case_id':case_state.get('case_id'),**core}); existing=next((x for x in case_state['candidate_derivations'] if x.get('fingerprint')==fp),None)
    if existing: return existing
    item={'record_type':'candidate_derivation','case_id':case_state.get('case_id'),'candidate_id':'C-'+fp[:12],'created':_now(),**core,'fingerprint':fp,'epistemic_state':'DERIVED','measurement_id':contract.get('measurement_id'),'metric_field':contract['metric_field'],'group_field':contract['group_field'],'source_expression':contract['source_expression']}
    case_state['candidate_derivations'].append(item); append_jsonl(CANDIDATE_DERIVATION_LEDGER_FILE,item)
    try:
        with _connect() as db:
            db.execute("INSERT OR IGNORE INTO candidate_derivations VALUES(?,?,?,?,?,?,?,?,?,?)",(item['case_id'],item['candidate_id'],item['created'],item['contract_id'],item['status'],_j(item['winners']),float(top),item['evidence_id'],_j(item['support_ids']),fp))
    except sqlite3.Error: pass
    return item


def assessment_from_active_contract(case_state):
    c=active_measurement_contract(case_state)
    if not c: return None
    d=derive_candidate_from_contract(case_state,c)
    if not d: return None
    base={'ranking_evidence_id':c['evidence_id'],'group_field':c['group_field'],'metric_field':c['metric_field'],'metric_justification':c.get('reason') or 'Active measurement contract.','support_ids':list(c.get('support_ids') or []),'requested_quantity':c.get('requested_quantity',''),'confidence':c.get('confidence','medium'),'reasoning_source':'deterministic_measurement_contract','evidence_version':int(case_state.get('evidence_version',0)),'winners':d['winners'],'maximum_value':d['maximum_value'],'measurement_contract_id':c['contract_id'],'measurement_id':c.get('measurement_id')}
    if d['status']=='TIED_MAXIMUM': return {'status':'AMBIGUOUS','candidate':None,'reason':'Active measurement contract produces a tied maximum: '+', '.join(d['winners']),'tie':True,**base}
    return {'status':'SUPPORTED','candidate':d['winners'][0],'reason':f"Python derived a unique maximum of {d['maximum_value']} under the active measurement contract.",'tie':False,**base}


def measurement_contract_violations(query,contract):
    if not contract: return []
    mappings=distinct_metric_mappings(query)
    if not mappings: return []
    groups=_group_fields(query); errs=[]
    if contract.get('group_field') not in groups: errs.append(f"Active measurement contract requires grouping by {contract.get('group_field')}")
    req=str(contract.get('source_expression') or '').strip().lower()
    if not any(str(m['source_expression']).strip().lower()==req for m in mappings): errs.append(f"Active measurement contract requires distinct_count({contract.get('source_expression')})")
    return errs


def compact_measurement_context(case_state, include_values=True):
    lines=[]
    cs=measurement_candidates(case_state)
    if cs:
        lines.append('COMPLETE MEASUREMENTS (select by measurement_id only):')
        for c in cs[-10:]:
            vals=', '.join(f'{k}={v}' for k,v in sorted(c['values'].items())) if include_values else '[values hidden during semantic selection]'
            lines.append(f"- {c['measurement_id']}: {c['metric_field']} = dc({c['source_expression']}) BY {c['group_field']} from {c['evidence_id']}: {vals}")
    c=active_measurement_contract(case_state)
    if c: lines += ['ACTIVE MEASUREMENT CONTRACT:',f"- {c['contract_id']} -> {c.get('measurement_id')}: {c['metric_field']} = dc({c['source_expression']}) BY {c['group_field']} ({c['status']})"]
    return '\n'.join(lines)
