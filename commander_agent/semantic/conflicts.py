"""Evidence-backed semantic conflict detection for ALR legacy compatibility."""
from __future__ import annotations

from commander_agent.semantic.binding import resolve_semantic_bindings
from commander_agent.state.semantic_state import active_bindings, invalidate_binding, record_semantic_bindings
from commander_agent.state.evidence_store import active_measurement_contract, invalidate_active_measurement_contract


def _rows(payload):
    if not isinstance(payload, dict): return []
    xs = payload.get("events", payload.get("results", []))
    return [x for x in xs if isinstance(x, dict)] if isinstance(xs, list) else []


def detect_and_rebind(case_state, evidence, payload, metrics=None):
    """Detect a coarse error-category binding when evidence proves one-to-many detail.

    This is a generic schema/granularity check: it does not inspect winners or known
    answers. It only compares representation roles under the active Investigation Frame.
    """
    frame = case_state.get("investigation_frame") or {}
    if frame.get("measure_role") != "detailed_failure_description":
        return None
    bindings = active_bindings(case_state)
    selected = next((b for b in bindings if b.get("kind")=="measurement_field"), None)
    if not selected or selected.get("field") != "errorCode":
        return None
    rows = _rows(payload)
    if not rows or not any("errorMessage" in r for r in rows):
        return None
    mapping={}
    for row in rows:
        code=row.get("errorCode"); msg=row.get("errorMessage")
        if code in (None,"") or msg in (None,""): continue
        mapping.setdefault(str(code),set()).add(str(msg))
    coarse={k:sorted(v) for k,v in mapping.items() if len(v)>1}
    if not coarse:
        return None
    reason=(
        "Selected failure-category field is too coarse for the requested detailed-error role: "
        + "; ".join(f"{k} maps to {len(v)} distinct detailed descriptions" for k,v in sorted(coarse.items()))
    )
    conflict=invalidate_binding(case_state,selected.get("binding_id"),reason,support_ids=[str((evidence or {}).get("evidence_id") or "")])
    contract=active_measurement_contract(case_state)
    if contract and str(contract.get("source_expression"))==str(selected.get("field")):
        invalidate_active_measurement_contract(case_state,reason)
    if metrics is not None:
        metrics["semantic_binding_invalidations"] = metrics.get("semantic_binding_invalidations",0)+1
        metrics["semantic_conflicts_detected"] = metrics.get("semantic_conflicts_detected",0)+1
    replacements=resolve_semantic_bindings(frame, question=case_state.get("question",""))
    added=record_semantic_bindings(case_state,[b for b in replacements if b.get("kind")=="measurement_field" and b.get("field")!="errorCode"])
    return {"conflict":conflict,"replacement_bindings":added}
