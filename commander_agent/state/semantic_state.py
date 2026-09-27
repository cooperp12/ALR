"""Durable Investigation Frame / semantic binding state for ALR legacy compatibility."""
from __future__ import annotations

from datetime import datetime

from commander_agent.config import INVESTIGATION_FRAME_LEDGER_FILE, SEMANTIC_BINDING_LEDGER_FILE, SEMANTIC_CONFLICT_LEDGER_FILE
from commander_agent.state.io import append_jsonl, stable_hash


def ensure_semantic_state(case_state):
    case_state.setdefault("investigation_frame", None)
    case_state.setdefault("semantic_bindings", [])
    case_state.setdefault("semantic_conflicts", [])
    case_state.setdefault("semantic_relationships", [])
    return case_state


def record_investigation_frame(case_state, frame):
    ensure_semantic_state(case_state)
    item={"record_type":"investigation_frame","case_id":case_state.get("case_id"),"created":datetime.now().isoformat(),**dict(frame or {})}
    item["frame_id"]="IF-"+stable_hash({k:v for k,v in item.items() if k not in {"created","frame_id"}})[:12]
    case_state["investigation_frame"]=item
    append_jsonl(INVESTIGATION_FRAME_LEDGER_FILE,item)
    return item


def record_semantic_bindings(case_state, bindings):
    ensure_semantic_state(case_state)
    existing={b.get("fingerprint") for b in case_state["semantic_bindings"]}
    added=[]
    for raw in bindings or []:
        b={"record_type":"semantic_binding","case_id":case_state.get("case_id"),"created":datetime.now().isoformat(),**dict(raw)}
        if b.get("fingerprint") in existing: continue
        case_state["semantic_bindings"].append(b); existing.add(b.get("fingerprint")); append_jsonl(SEMANTIC_BINDING_LEDGER_FILE,b); added.append(b)
    return added


def active_bindings(case_state):
    ensure_semantic_state(case_state)
    return [dict(b) for b in case_state["semantic_bindings"] if b.get("status")=="SUPPORTED"]


def invalidate_binding(case_state,binding_id,reason,conflict_type="SEMANTIC_BINDING_CONFLICT",support_ids=None):
    ensure_semantic_state(case_state)
    target=next((b for b in case_state["semantic_bindings"] if b.get("binding_id")==binding_id and b.get("status")=="SUPPORTED"),None)
    if not target: return None
    target["status"]="INVALIDATED"; target["invalidated_reason"]=str(reason or "")
    conflict={"record_type":"semantic_conflict","case_id":case_state.get("case_id"),"created":datetime.now().isoformat(),"conflict_id":"SC-"+stable_hash({"binding_id":binding_id,"reason":reason,"type":conflict_type})[:12],"type":conflict_type,"binding_id":binding_id,"reason":str(reason or ""),"support_ids":list(support_ids or []),"status":"OPEN"}
    case_state["semantic_conflicts"].append(conflict); append_jsonl(SEMANTIC_CONFLICT_LEDGER_FILE,conflict); return conflict


def compact_semantic_state(case_state):
    ensure_semantic_state(case_state)
    f=case_state.get("investigation_frame") or {}
    lines=[]
    if f:
        lines.append("INVESTIGATION FRAME:")
        lines.append(f"- domain={f.get('domain')} measure={f.get('measure_concept')} role={f.get('measure_role')} aggregation={f.get('aggregation')} ranking={f.get('ranking')} output={f.get('requested_output_concept')}")
        lines.append("- primitives="+", ".join(f.get("primitives") or []))
    bs=active_bindings(case_state)
    if bs:
        lines.append("ACTIVE SEMANTIC BINDINGS:")
        for b in bs:
            target=b.get("field") or b.get("value") or "[unbound]"
            lines.append(f"- {b.get('binding_id')}: {b.get('concept')} -> {target}; kind={b.get('kind')}; role={b.get('role_required')}")
    if case_state.get("semantic_conflicts"):
        lines.append("SEMANTIC CONFLICTS:")
        for c in case_state["semantic_conflicts"][-3:]: lines.append(f"- {c.get('conflict_id')}: {c.get('type')} {c.get('reason')}")
    return "\n".join(lines)
