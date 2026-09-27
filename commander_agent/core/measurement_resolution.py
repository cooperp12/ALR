"""Narrow semantic selection for ALR legacy compatibility measurement contracts.

The model selects only a stable measurement_id plus reason/confidence. Python owns
field/provenance reconstruction, contract creation, candidate derivation and ties.
"""
from __future__ import annotations

import json

from commander_agent.mcp.results import clip_text
from commander_agent.reasoning.local_ollama import structured_chat, message_content
from commander_agent.skills.registry import load_skill_instructions
from commander_agent.skills.structured_output_validation.scripts.schema import parse_model_object
from commander_agent.state.evidence_store import (
    active_measurement_contract,
    compact_measurement_context,
    create_measurement_contract,
    measurement_by_id,
    measurement_candidates,
)
from commander_agent.state.semantic_state import active_bindings, compact_semantic_state

SCHEMA={
    "type":"object",
    "required":["selected_measurement_id","reason","confidence"],
    "additionalProperties":False,
    "properties":{
        "selected_measurement_id":{"type":["string","null"]},
        "reason":{"type":"string","maxLength":700},
        "confidence":{"type":"string","enum":["high","medium","low"]},
    },
}


def _ranking(q):
    q=str(q or '').lower()
    return any(x in q for x in ('most','highest','largest','maximum')) and any(x in q for x in ('distinct','unique'))


def _events(state):
    lines=[]
    evidence_ids=[]
    for e in reversed(state.get('evidence',[])):
        if e.get('source') in {'skill_reasoning','deterministic'}:
            continue
        p=e.get('machine_result')
        rows=p.get('events',p.get('results',[])) if isinstance(p,dict) else []
        if not isinstance(rows,list):
            continue
        chosen=[]
        for r in rows[:8]:
            if not isinstance(r,dict):
                continue
            d={k:r[k] for k in (
                'userIdentity.accessKeyId','userIdentity.userName','eventName','errorCode',
                'errorMessage','raw_code','raw_message'
            ) if k in r}
            if d:
                chosen.append(d)
        if chosen:
            eid=str(e.get('evidence_id') or '')
            evidence_ids.append(eid)
            lines.append(f"{eid} ({e.get('method_class','')}):")
            lines += ['  '+clip_text(json.dumps(x,ensure_ascii=False),650) for x in chosen]
        if len(evidence_ids)>=3:
            break
    return '\n'.join(lines)


def _prompt(q,state):
    instructions=load_skill_instructions('measurement_semantics_resolution')
    return f'''You are executing the ALR legacy compatibility measurement_semantics_resolution skill.

{instructions}

QUESTION:
{q}

{compact_semantic_state(state) or 'SEMANTIC STATE: [none]'}

{compact_measurement_context(state, include_values=False) or 'COMPLETE MEASUREMENTS: [none]'}

REPRESENTATIVE MACHINE EVENTS:
{_events(state) or '[none]'}

Return exactly the three fields required by the skill contract. The selected ID must be copied exactly from COMPLETE MEASUREMENTS.'''


def resolve_measurement_semantics(*,question,case_state,metrics):
    active=active_measurement_contract(case_state)
    if active or not _ranking(question):
        return active
    candidates=measurement_candidates(case_state)

    # ALR legacy compatibility: if the semantic layer already bound the measurement concept before
    # query execution, Python promotes the matching complete measurement directly.
    # Result values never participate in this semantic decision.
    bindings=active_bindings(case_state)
    measure_binding=next((b for b in bindings if b.get('kind')=='measurement_field'),None)
    group_binding=next((b for b in bindings if b.get('kind')=='group_field'),None)
    if measure_binding:
        matches=[c for c in candidates if str(c.get('source_expression'))==str(measure_binding.get('field'))]
        if group_binding:
            matches=[c for c in matches if str(c.get('group_field'))==str(group_binding.get('field'))]
        if len(matches)==1:
            selected=matches[0]
            contract=create_measurement_contract(
                case_state,
                requested_quantity=question,
                measurement_id=selected.get('measurement_id'),
                reason=f"Semantic binding {measure_binding.get('binding_id')} selected role {measure_binding.get('role_required')}: {measure_binding.get('reason','')}",
                confidence=measure_binding.get('confidence','high'),
            )
            metrics['measurement_contracts_created']=metrics.get('measurement_contracts_created',0)+1
            metrics['semantic_binding_contract_promotions']=metrics.get('semantic_binding_contract_promotions',0)+1
            case_state['measurement_resolution_last']={
                'status':'RESOLVED_FROM_SEMANTIC_BINDING',
                'selected_measurement_id':selected.get('measurement_id'),
                'semantic_binding_id':measure_binding.get('binding_id'),
                'contract_id':contract.get('contract_id'),
            }
            print('\n'+'='*70+'\nMEASUREMENT CONTRACT (SEMANTIC BINDING)\n'+'='*70+'\n'+json.dumps(contract,ensure_ascii=False,indent=2))
            return contract

    if len({c.get('measurement_id') for c in candidates})<2:
        return None

    metrics['measurement_resolution_calls']=metrics.get('measurement_resolution_calls',0)+1
    try:
        response=structured_chat(
            role='investigator',
            messages=[{'role':'user','content':_prompt(question,case_state)}],
            schema=SCHEMA,
            metrics=metrics,
            purpose='measurement_semantics',
            think='low',
        )
        ok,obj,errs=parse_model_object(message_content(response),SCHEMA)
        if not ok:
            metrics['measurement_resolution_failures']=metrics.get('measurement_resolution_failures',0)+1
            print('[Measurement semantics unresolved: '+clip_text('; '.join(errs),400)+']')
            return None

        selected_id=str(obj.get('selected_measurement_id') or '').strip()
        if not selected_id:
            metrics['measurement_resolution_unresolved']=metrics.get('measurement_resolution_unresolved',0)+1
            case_state['measurement_resolution_last']={**obj,'status':'AMBIGUOUS'}
            print('[Measurement semantics unresolved: model selected no complete measurement]')
            return None

        selected=measurement_by_id(case_state,selected_id)
        if not selected:
            metrics['measurement_resolution_failures']=metrics.get('measurement_resolution_failures',0)+1
            known=', '.join(c.get('measurement_id','') for c in candidates)
            print('[Measurement semantics rejected: selected_measurement_id is not an existing complete measurement; known IDs: '+clip_text(known,300)+']')
            return None

        # Python reconstructs the requested quantity, evidence ID, fields, support IDs,
        # observation provenance and measured values from the selected measurement ID.
        contract=create_measurement_contract(
            case_state,
            requested_quantity=question,
            measurement_id=selected_id,
            reason=obj.get('reason',''),
            confidence=obj.get('confidence','medium'),
        )
        metrics['measurement_contracts_created']=metrics.get('measurement_contracts_created',0)+1
        case_state['measurement_resolution_last']={
            **obj,
            'status':'RESOLVED',
            'measurement_id':selected_id,
            'evidence_id':contract.get('evidence_id'),
            'contract_id':contract.get('contract_id'),
        }
        print('\n'+'='*70+'\nMEASUREMENT CONTRACT\n'+'='*70+'\n'+json.dumps(contract,ensure_ascii=False,indent=2))
        return contract
    except Exception as exc:
        metrics['measurement_resolution_failures']=metrics.get('measurement_resolution_failures',0)+1
        print(f'[Measurement semantics unavailable: {type(exc).__name__}: {clip_text(str(exc),320)}]')
        return None
