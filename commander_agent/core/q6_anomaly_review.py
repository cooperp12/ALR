from __future__ import annotations

"""Bounded worker/supervisor review for Q6 deterministic anomalies.

The models are an exception-control plane, not the source of truth.  They receive a
small machine-evidence packet only after Python detects a contradiction.  They may
recommend one action from a fixed allow-list, but Python invariants decide what is
actually executed.  No benchmark answer/oracle is ever included in these prompts.
"""

import json
from typing import Any

from commander_agent.reasoning.local_ollama import structured_chat, message_content
from commander_agent.skills.structured_output_validation.scripts.schema import parse_model_object

ACTIONS = (
    "switch_to_raw_recovery",
    "verify_raw_boundary",
    "reject_candidate_and_recompute_first",
    "retry_authoritative_enrichment",
    "continue_deterministic_block",
)

ANOMALY_DEFAULT_ACTION = {
    "source_route_disagreement": "switch_to_raw_recovery",
    "chronology_invariant_failure": "reject_candidate_and_recompute_first",
    "independent_validation_conflict": "verify_raw_boundary",
    "external_enrichment_failure": "continue_deterministic_block",
}

WORKER_SCHEMA = {
    "type": "object",
    "required": ["diagnosis", "recommended_action", "confidence", "reason", "checks"],
    "properties": {
        "diagnosis": {"type": "string", "maxLength": 120},
        "recommended_action": {"type": "string", "enum": list(ACTIONS)},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "reason": {"type": "string", "maxLength": 320},
        "checks": {"type": "array", "items": {"type": "string", "maxLength": 180}, "maxItems": 5},
    },
}

SUPERVISOR_SCHEMA = {
    "type": "object",
    "required": ["decision", "approved_action", "confidence", "reason", "invariants"],
    "properties": {
        "decision": {"type": "string", "enum": ["approve", "modify", "deny"]},
        "approved_action": {"type": "string", "enum": [*ACTIONS, "none"]},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "reason": {"type": "string", "maxLength": 320},
        "invariants": {"type": "array", "items": {"type": "string", "maxLength": 180}, "maxItems": 6},
    },
}


def _compact(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return "[depth-limited]"
    if isinstance(value, dict):
        out = {}
        for key, item in list(value.items())[:30]:
            # Never route benchmark/scorer internals into reasoning prompts.
            if str(key).lower() in {"expected", "expected_output", "expected_patterns", "score_oracle", "oracle"}:
                continue
            out[str(key)] = _compact(item, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_compact(x, depth=depth + 1) for x in list(value)[:20]]
    text = str(value) if value is not None else None
    if isinstance(text, str) and len(text) > 700:
        return text[:700] + "...[truncated]"
    return value


def _parse(response, schema):
    ok, obj, errors = parse_model_object(message_content(response), schema)
    if ok:
        return obj, []
    return None, errors


def review_q6_anomaly(kind: str, packet: dict[str, Any], metrics: dict | None = None) -> dict[str, Any]:
    """Ask both local roles to review a deterministic anomaly.

    The returned ``deterministic_action`` is selected from the anomaly policy, not
    from model text.  Model output is advisory telemetry used to detect bad recovery
    assumptions and to explain why a bounded route is appropriate.
    """
    metrics = metrics if metrics is not None else {}
    default_action = ANOMALY_DEFAULT_ACTION.get(kind, "continue_deterministic_block")
    compact = _compact(packet)
    result: dict[str, Any] = {
        "kind": kind,
        "deterministic_action": default_action,
        "worker": None,
        "supervisor": None,
        "model_status": "not_run",
    }
    metrics["q6_anomaly_escalations"] = metrics.get("q6_anomaly_escalations", 0) + 1

    worker_prompt = f"""
You are the Q6 anomaly WORKER. Do not answer the benchmark question and do not infer
an Ubuntu codename. Diagnose only the deterministic pipeline contradiction below.
Return exactly one JSON object matching the schema.

Rules:
- Treat machine evidence as authoritative.
- Do not weaken identity, chronology, AMI+region, or independent-verification invariants.
- Do not invent values that are absent from the packet.
- Recommend only one bounded recovery action from the schema.
- If a parsed route says zero while raw evidence has matches, identify a source/extraction-route problem rather than declaring no evidence.
- FIRST means the minimum matching event time over the complete candidate population; presentation limits must never redefine it.

ANOMALY KIND: {kind}
DETERMINISTIC SAFE DEFAULT: {default_action}
MACHINE PACKET:
{json.dumps(compact, ensure_ascii=False, indent=2)}
""".strip()

    try:
        response = structured_chat(
            role="investigator",
            messages=[{"role": "user", "content": worker_prompt}],
            schema=WORKER_SCHEMA,
            metrics=metrics,
            purpose="q6_anomaly_worker",
            num_predict=550,
            think="low",
        )
        worker, errors = _parse(response, WORKER_SCHEMA)
        if worker is None:
            result["worker_error"] = "; ".join(errors[:3])
        else:
            result["worker"] = worker
            metrics["q6_worker_anomaly_reviews"] = metrics.get("q6_worker_anomaly_reviews", 0) + 1
    except Exception as exc:
        result["worker_error"] = f"{type(exc).__name__}: {exc}"

    supervisor_prompt = f"""
You are the Q6 anomaly SUPERVISOR. You do not solve the benchmark question. Review a
worker diagnosis of a deterministic contradiction and decide whether its proposed
recovery preserves the investigation invariants. Return exactly one JSON object.

Rules:
- Never output a candidate benchmark answer, Ubuntu codename, or guessed AMI.
- Python machine evidence and deterministic invariants remain authoritative.
- FIRST is a global minimum over the complete matching population before display limits.
- A verification method known to be extraction-broken cannot independently validate a raw recovery.
- Identity expansion must remain evidence-derived.
- Approve or modify only to one action in the supplied schema; deny if evidence is insufficient.

ANOMALY KIND: {kind}
DETERMINISTIC SAFE DEFAULT: {default_action}
MACHINE PACKET:
{json.dumps(compact, ensure_ascii=False, indent=2)}
WORKER REVIEW:
{json.dumps(result.get('worker'), ensure_ascii=False, indent=2)}
""".strip()

    try:
        response = structured_chat(
            role="supervisor",
            messages=[{"role": "user", "content": supervisor_prompt}],
            schema=SUPERVISOR_SCHEMA,
            metrics=metrics,
            purpose="q6_anomaly_supervisor",
            num_predict=450,
            think=False,
        )
        supervisor, errors = _parse(response, SUPERVISOR_SCHEMA)
        if supervisor is None:
            result["supervisor_error"] = "; ".join(errors[:3])
        else:
            result["supervisor"] = supervisor
            metrics["q6_supervisor_anomaly_reviews"] = metrics.get("q6_supervisor_anomaly_reviews", 0) + 1
    except Exception as exc:
        result["supervisor_error"] = f"{type(exc).__name__}: {exc}"

    if result.get("worker") and result.get("supervisor"):
        result["model_status"] = "complete"
    elif result.get("worker") or result.get("supervisor"):
        result["model_status"] = "partial"
    else:
        result["model_status"] = "unavailable"

    # The models are advisory.  Record whether they agree with the deterministic
    # safe action, but never let a model bypass a Python validation invariant.
    worker_action = (result.get("worker") or {}).get("recommended_action")
    supervisor_action = (result.get("supervisor") or {}).get("approved_action")
    result["worker_agrees_with_default"] = worker_action == default_action
    result["supervisor_agrees_with_default"] = supervisor_action == default_action
    return result
