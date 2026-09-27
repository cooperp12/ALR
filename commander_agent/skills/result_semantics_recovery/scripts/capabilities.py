from __future__ import annotations

import json
from typing import Any
from commander_agent.core.bounded_state import short
from commander_agent.state.candidate_checks import candidate_errors

from commander_agent.config import MAX_SEMANTIC_FORMAT_REPAIRS
from commander_agent.mcp.results import clip_text
from commander_agent.skills.structured_output_validation.scripts.schema import parse_model_object
from commander_agent.state.evidence import current_evidence_version
from commander_agent.state.evidence_store import assessment_from_active_contract
from commander_agent.state.scope import render_scope_contract
from commander_agent.state.orchestration import RECOVERY_CAPABILITIES
from commander_agent.reasoning.local_ollama import structured_chat, message_content


COMMON_SUGGESTIONS = {
    "type": "array",
    "items": {"type": "string", "enum": RECOVERY_CAPABILITIES},
}

CAPABILITY_SCHEMAS: dict[str, dict[str, Any]] = {
    "semantic_field_review": {
        "type": "object",
        "required": ["verdict", "reason", "support_ids", "suggested_capabilities"],
        "properties": {
            "verdict": {"type": "string", "enum": ["ADEQUATE", "INADEQUATE", "UNKNOWN"]},
            "reason": {"type": "string"},
            "support_ids": {"type": "array", "items": {"type": "string"}},
            "suggested_capabilities": COMMON_SUGGESTIONS,
        },
    },
    "schema_inspection": {
        "type": "object",
        "required": ["action", "reason", "recommended_spl", "support_ids", "suggested_capabilities"],
        "properties": {
            "action": {"type": "string", "enum": ["RUN_QUERY", "DEFER"]},
            "reason": {"type": "string"},
            "recommended_spl": {"type": ["string", "null"]},
            "support_ids": {"type": "array", "items": {"type": "string"}},
            "suggested_capabilities": COMMON_SUGGESTIONS,
        },
    },
    "alternative_aggregation": {
        "type": "object",
        "required": ["action", "reason", "recommended_spl", "support_ids", "suggested_capabilities"],
        "properties": {
            "action": {"type": "string", "enum": ["RUN_QUERY", "DEFER"]},
            "reason": {"type": "string"},
            "recommended_spl": {"type": ["string", "null"]},
            "support_ids": {"type": "array", "items": {"type": "string"}},
            "suggested_capabilities": COMMON_SUGGESTIONS,
        },
    },
    "relationship_analysis": {
        "type": "object",
        "required": ["action", "reason", "recommended_spl", "support_ids", "suggested_capabilities"],
        "properties": {
            "action": {"type": "string", "enum": ["RUN_QUERY", "DEFER"]},
            "reason": {"type": "string"},
            "recommended_spl": {"type": ["string", "null"]},
            "support_ids": {"type": "array", "items": {"type": "string"}},
            "suggested_capabilities": COMMON_SUGGESTIONS,
        },
    },
    "temporal_pattern_analysis": {
        "type": "object",
        "required": ["action", "reason", "analysis_request", "support_ids", "suggested_capabilities"],
        "properties": {
            "action": {"type": "string", "enum": ["RUN_ANALYSIS", "DEFER"]},
            "reason": {"type": "string"},
            "analysis_request": {
                "type": ["object", "null"],
                "properties": {
                    "group_field": {"type": ["string", "null"]},
                    "value_field": {"type": ["string", "null"]},
                    "time_field": {"type": "string"},
                    "measures": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": [
                                "event_count",
                                "distinct_count",
                                "cumulative_distinct_count",
                                "new_distinct_count",
                            ],
                        },
                    },
                    "comparison_feature": {"type": ["string", "null"]},
                },
            },
            "support_ids": {"type": "array", "items": {"type": "string"}},
            "suggested_capabilities": COMMON_SUGGESTIONS,
        },
    },
    "scope_expansion": {
        "type": "object",
        "required": [
            "action",
            "reason",
            "recommended_spl",
            "scope_change_reason",
            "support_ids",
            "suggested_capabilities",
        ],
        "properties": {
            "action": {"type": "string", "enum": ["PROPOSE_SCOPE_CHANGE", "DEFER"]},
            "reason": {"type": "string"},
            "recommended_spl": {"type": ["string", "null"]},
            "scope_change_reason": {"type": ["string", "null"]},
            "support_ids": {"type": "array", "items": {"type": "string"}},
            "suggested_capabilities": COMMON_SUGGESTIONS,
        },
    },
}

CANDIDATE_ASSESSMENT_SCHEMA = {
    "type": "object",
    "required": ["status", "candidate", "support_ids", "reason", "requested_quantity", "confidence"],
    "properties": {
        "status": {"type": "string", "enum": ["SUPPORTED", "AMBIGUOUS", "CONFLICT"]},
        "candidate": {"type": ["string", "null"]},
        "support_ids": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
        "requested_quantity": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
}

for _schema in CAPABILITY_SCHEMAS.values():
    _schema["properties"]["reason"]["maxLength"] = 360
    if "action" in _schema["properties"]:
        _schema["properties"]["defer_kind"] = {"type": "string", "enum": ["missing_prerequisite", "not_applicable"]}
        _schema["properties"]["prerequisites"] = COMMON_SUGGESTIONS
CANDIDATE_ASSESSMENT_SCHEMA["properties"].update({
    "ranking_evidence_id": {"type": ["string", "null"]},
    "group_field": {"type": ["string", "null"]},
    "metric_field": {"type": ["string", "null"]},
    "metric_justification": {"type": ["string", "null"], "maxLength": 500},
})
CANDIDATE_ASSESSMENT_SCHEMA["properties"]["reason"]["maxLength"] = 500

CAPABILITY_INSTRUCTIONS = {
    "semantic_field_review": """
Your ONLY job is to judge whether the current field/representation has the right semantic granularity for the concept in the question.
Do not write SPL. Do not choose a candidate. Do not change scope. Return ADEQUATE, INADEQUATE, or UNKNOWN and suggest sibling capabilities only when useful.
A tie under a coarse categorical field does not prove that the underlying concept is tied. Do not use event count as a tiebreaker for a distinct-value question.
""",
    "schema_inspection": """
Your ONLY job is to propose one focused same-scope SPL query that inspects representative fields/events needed to understand the unresolved concept.
Do not answer the question. Do not broaden population/source scope. Prefer a small table/field comparison over a broad raw export. You must obtain the missing schema evidence yourself. DEFER only if no valid scoped query can obtain it or the method is inapplicable.
""",
    "alternative_aggregation": """
Your ONLY job is to propose one materially different same-scope calculation of the requested quantity.
Do not simply repeat or cosmetically rewrite an existing query. Preserve all current population/source constraints. Design the query that obtains missing evidence; do not defer merely because it has not been run. DEFER only for a concrete prerequisite or inapplicability.
""",
    "relationship_analysis": """
Your ONLY job is to propose one same-scope SPL query that examines relationships among the requested entity and relevant resources/actions/attributes when those relationships can reduce the current ambiguity.
Do not repeat an existing aggregation. Do not broaden scope. Design the query that obtains missing relationship evidence; its absence is not a blocker. DEFER only for a concrete prerequisite or inapplicability.
""",
    "temporal_pattern_analysis": """
Your ONLY job is to specify a deterministic temporal analysis request. Do not write SPL and do not calculate trends yourself.
Choose group/value/time fields and generic measures only when time-series/diversity behaviour can materially address the current gap. Otherwise DEFER.
""",
    "scope_expansion": """
Your ONLY job is to propose a genuine, explicit population/source scope change after same-scope routes have been exhausted or ruled irrelevant.
The proposed SPL must actually differ from the current scope. Do not request expansion merely to break a tie. If expansion is not justified, DEFER.
""",
}


def _message_text(response):
    return message_content(response)


def _add_usage(metrics, response):
    prompt_tokens = getattr(response, "prompt_eval_count", None)
    output_tokens = getattr(response, "eval_count", None)
    if isinstance(response, dict):
        prompt_tokens = response.get("prompt_eval_count", prompt_tokens)
        output_tokens = response.get("eval_count", output_tokens)
    if prompt_tokens is not None:
        metrics["ollama_prompt_tokens"] = metrics.get("ollama_prompt_tokens", 0) + int(prompt_tokens or 0)
    if output_tokens is not None:
        metrics["ollama_output_tokens"] = metrics.get("ollama_output_tokens", 0) + int(output_tokens or 0)


def _data_evidence_digest(case_state, max_items=4, excerpt_chars=1000):
    records = [
        r for r in case_state.get("evidence", [])
        if r.get("source") not in {"skill_reasoning", "deterministic"}
    ][-max_items:]
    parts = []
    for record in records:
        parts.append(
            f"{record.get('evidence_id')} | version={record.get('evidence_version')} | method={record.get('method_class')}\n"
            f"QUERY: {clip_text(record.get('query', ''), 700)}\n"
            f"RESULT: {clip_text(record.get('result_excerpt', ''), excerpt_chars)}"
        )
    return "\n\n".join(parts)


def _known_support_ids(case_state, requested):
    known = {
        str(item.get("evidence_id"))
        for item in case_state.get("evidence", [])
        if item.get("source") not in {"skill_reasoning", "deterministic"}
        and item.get("evidence_id")
    }
    return [str(x) for x in (requested or []) if str(x) in known]


def _capability_prompt(capability, question, case_state, supervisor_feedback=None, current_candidate=""):
    state = case_state.get("orchestration") or {}
    gap = str((supervisor_feedback or {}).get("remaining_gap") or state.get("remaining_gap") or "Resolve the current evidence gap.")
    return f"""
You are the Investigator executing ONE narrowly bounded capability: {capability}.

{CAPABILITY_INSTRUCTIONS[capability].strip()}

Rules:
- Use only the question and machine evidence below; never use remembered challenge/scorer answers.
- Preserve the current scope contract unless this capability is scope_expansion.
- Return exactly one JSON object matching the supplied tiny schema. No prose outside JSON.
- Keep the reason short. Do not restate all evidence.
- DEFER requires defer_kind and, for missing_prerequisite, a non-empty prerequisites list naming the capability that must obtain it. A dependency cannot name this capability.
- support_ids may reference only evidence IDs shown below.

QUESTION:
{question}

CURRENT GAP:
{short(gap)}

CURRENT CANDIDATE:
{current_candidate or '[none]'}

SCOPE CONTRACT:
{render_scope_contract(case_state.get('scope_contract'))}

MACHINE EVIDENCE:
{_data_evidence_digest(case_state) or '[none]'}
""".strip()


def _repair_prompt(capability, previous_text, errors):
    return f"""
Repair JSON for capability {capability} only. Do not change the investigative idea and do not add facts.
Errors:
{chr(10).join('- ' + str(x) for x in errors[:6])}
Return one JSON object matching the supplied schema and nothing else.

Previous output:
{clip_text(previous_text, 4000)}
""".strip()


def normalise_capability_result(capability, obj, case_state, source="local"):
    """Convert a tiny capability result into the legacy orchestration action shape.

    This function is intentionally mechanical. It does not interpret domain evidence.
    """
    obj = dict(obj or {})
    support_ids = _known_support_ids(case_state, obj.get("support_ids"))
    suggestions = [x for x in (obj.get("suggested_capabilities") or []) if x in RECOVERY_CAPABILITIES and x != capability]
    base = {
        "capability": capability,
        "candidate_status": "not_evaluated",
        "candidate": None,
        "recommended_spl": None,
        "support_ids": support_ids,
        "suggested_capabilities": suggestions,
        "reason": short(obj.get("reason")),
        "scope_change": {"requested": False, "reason": None},
        "confidence": "medium",
        "issue_type": "",
        "requested_quantity": "",
        "observed_semantics": {},
        "unsupported_claims": [],
        "supported_facts": [],
        "next_action": "",
        "reasoning_source": source,
        "evidence_version": current_evidence_version(case_state),
    }

    if capability == "semantic_field_review":
        verdict = str(obj.get("verdict") or "UNKNOWN")
        base["decision"] = "SEMANTIC_FINDING"
        base["issue_type"] = "semantic_field_" + verdict.lower()
        base["reason"] = f"Field-semantics verdict={verdict}. {base['reason']}".strip()
        base["observed_semantics"] = {"field_verdict": verdict}
        return base

    action = str(obj.get("action") or "DEFER")
    if action == "DEFER":
        base["defer_kind"] = obj.get("defer_kind")
        base["prerequisites"] = [x for x in obj.get("prerequisites", []) if x in RECOVERY_CAPABILITIES and x != capability]
        base["decision"] = "DEFER_CAPABILITY"
        return base

    if capability == "schema_inspection":
        base["decision"] = "INVESTIGATE_SCHEMA"
        base["recommended_spl"] = str(obj.get("recommended_spl") or "").strip() or None
    elif capability == "alternative_aggregation":
        base["decision"] = "RUN_ALTERNATIVE_QUERY"
        base["recommended_spl"] = str(obj.get("recommended_spl") or "").strip() or None
    elif capability == "relationship_analysis":
        base["decision"] = "RUN_CHECK_QUERY"
        base["recommended_spl"] = str(obj.get("recommended_spl") or "").strip() or None
    elif capability == "temporal_pattern_analysis":
        base["decision"] = "RUN_TEMPORAL_ANALYSIS"
        base["analysis_request"] = obj.get("analysis_request")
    elif capability == "scope_expansion":
        base["decision"] = "EXPAND_SCOPE"
        base["recommended_spl"] = str(obj.get("recommended_spl") or "").strip() or None
        base["scope_change"] = {
            "requested": True,
            "reason": str(obj.get("scope_change_reason") or obj.get("reason") or "").strip() or None,
        }
    return base


def validate_normalised_capability_result(capability, result):
    decision = str(result.get("decision") or "")
    if decision == "SEMANTIC_FINDING":
        return []
    if decision == "DEFER_CAPABILITY":
        if result.get("defer_kind") not in {"missing_prerequisite", "not_applicable"}:
            return ["DEFER requires a concrete defer_kind"]
        if result.get("defer_kind") == "missing_prerequisite" and not result.get("prerequisites"):
            return ["Missing prerequisite requires another named capability"]
        return []
    errors = []
    if capability in {"schema_inspection", "alternative_aggregation", "relationship_analysis", "scope_expansion"}:
        if not str(result.get("recommended_spl") or "").strip():
            errors.append(f"{capability} requires executable recommended_spl")
    if capability == "temporal_pattern_analysis":
        req = result.get("analysis_request")
        if not isinstance(req, dict):
            errors.append("temporal_pattern_analysis requires analysis_request")
        else:
            if not str(req.get("time_field") or "").strip():
                errors.append("temporal analysis requires time_field")
            if not isinstance(req.get("measures"), list) or not req.get("measures"):
                errors.append("temporal analysis requires at least one measure")
    if capability == "scope_expansion":
        if not str((result.get("scope_change") or {}).get("reason") or "").strip():
            errors.append("scope_expansion requires a non-empty scope_change reason")
    return errors


def review_capability(
    *, capability, question, case_state, metrics, supervisor_feedback=None,
    current_candidate="", reasoner=None,
):
    """Run one small capability-specific Investigator contract.

    Each capability receives its own tiny schema. There is no shared mega-schema and no
    cross-capability action repair. Invalid output fails only this child route.
    """
    schema = CAPABILITY_SCHEMAS.get(capability)
    if schema is None:
        return None
    metrics["capability_review_calls"] = metrics.get("capability_review_calls", 0) + 1
    metrics["semantic_skill_reviews"] = metrics.get("semantic_skill_reviews", 0) + 1
    prompt = _capability_prompt(capability, question, case_state, supervisor_feedback, current_candidate)

    def parse(text, source):
        ok, obj, errors = parse_model_object(text, schema)
        if not ok:
            return None, errors
        result = normalise_capability_result(capability, obj, case_state, source=source)
        contract_errors = validate_normalised_capability_result(capability, result)
        if contract_errors:
            return None, contract_errors
        return result, []

    text = ""
    errors = []
    try:
        response = structured_chat(
            role="investigator",
            messages=[{"role": "user", "content": prompt}],
            schema=schema,
            metrics=metrics,
            purpose=f"capability_{capability}",
        )
        text = _message_text(response)
        result, errors = parse(text, "local_capability")
        if result is not None:
            return result
        metrics["structured_output_rejections"] = metrics.get("structured_output_rejections", 0) + 1
        print(f"[Capability {capability} output rejected: " + "; ".join(errors[:3]) + "]")
    except Exception as exc:
        errors = [f"{type(exc).__name__}: {exc}"]
        print(f"[Capability {capability} warning: {type(exc).__name__}: {exc}]")

    for _ in range(MAX_SEMANTIC_FORMAT_REPAIRS):
        if not text:
            break
        metrics["capability_format_repairs"] = metrics.get("capability_format_repairs", 0) + 1
        try:
            response = structured_chat(
                role="investigator",
                messages=[{"role": "user", "content": _repair_prompt(capability, text, errors)}],
                schema=schema,
                metrics=metrics,
                purpose=f"capability_{capability}_format_repair",
                num_ctx=2048,
                num_predict=900,
                think=False,
            )
            repaired = _message_text(response)
            result, errors = parse(repaired, "local_capability_repair")
            if result is not None:
                metrics["capability_format_repair_successes"] = metrics.get("capability_format_repair_successes", 0) + 1
                return result
            metrics["structured_output_rejections"] = metrics.get("structured_output_rejections", 0) + 1
        except Exception as exc:
            errors = [f"{type(exc).__name__}: {exc}"]

    if reasoner is not None and reasoner.available:
        remote = reasoner.fast(prompt, metrics=metrics, purpose=f"capability_{capability}", max_output_tokens=700, effort="low")
        if remote:
            result, errors = parse(remote, "remote_capability")
            if result is not None:
                return result

    metrics["capability_review_failures"] = metrics.get("capability_review_failures", 0) + 1
    if errors:
        print(f"[Capability {capability} unavailable: " + clip_text("; ".join(errors), 400) + "]")
    return None


def _candidate_prompt(question, case_state, current_candidate=""):
    return f"""
You are the Candidate Assessment stage. This is a narrow evidence interpretation task, not a recovery planner.

Return exactly one JSON object matching the supplied schema.
Rules:
- Use only the question and machine evidence below. Never use remembered challenge/scorer answers.
- SUPPORTED means one candidate is directly supported for the exact requested quantity under the evidence shown.
- If the evidence is tied, semantically coarse, contradictory, or insufficient, return AMBIGUOUS or CONFLICT with candidate=null.
- Do not use total event count to break a distinct-value tie unless the question explicitly asks for event count.
- support_ids may reference only evidence IDs shown below.
- Do not write SPL and do not suggest recovery actions.
- For a SUPPORTED maximum/ranking, supply ranking_evidence_id, group_field, metric_field and metric_justification.
- Select the measurement because it matches the question, never because it produces a unique winner. Compare error code, error message and operation/error meanings using representative events. Changing identifiers alone may inflate message diversity.
- Excerpts may be clipped. Do not claim completeness from an excerpt; the controller checks full structured rows.

QUESTION:
{question}

CURRENT CANDIDATE FROM CALLER:
{current_candidate or '[none]'}

SCOPE CONTRACT:
{render_scope_contract(case_state.get('scope_contract'))}

MACHINE EVIDENCE:
{_data_evidence_digest(case_state, max_items=4, excerpt_chars=1000) or '[none]'}
""".strip()


def normalise_candidate_assessment(obj, case_state, source="local_candidate_assessment"):
    obj = dict(obj or {})
    status = str(obj.get("status") or "AMBIGUOUS")
    candidate = str(obj.get("candidate") or "").strip() or None
    if status != "SUPPORTED":
        candidate = None
    return {
        "status": status,
        "candidate": candidate,
        **{k: obj.get(k) for k in ("ranking_evidence_id", "group_field", "metric_field", "metric_justification")},
        "support_ids": _known_support_ids(case_state, obj.get("support_ids")),
        "reason": short(obj.get("reason")),
        "requested_quantity": str(obj.get("requested_quantity") or "").strip(),
        "confidence": str(obj.get("confidence") or "medium"),
        "reasoning_source": source,
        "evidence_version": current_evidence_version(case_state),
    }


def assess_candidate(*, question, case_state, metrics, current_candidate="", reasoner=None):
    metrics["candidate_assessment_calls"] = metrics.get("candidate_assessment_calls", 0) + 1
    case_state.setdefault("question", question)
    contracted = assessment_from_active_contract(case_state)
    if contracted:
        errors = candidate_errors(contracted, case_state)
        if not errors:
            metrics["deterministic_candidate_derivations"] = metrics.get("deterministic_candidate_derivations", 0) + 1
            return contracted
        metrics["candidate_semantic_rejections"] = metrics.get("candidate_semantic_rejections", 0) + 1
        return {**contracted, "status": "AMBIGUOUS", "candidate": None, "reason": "; ".join(errors), "semantic_errors": errors}
    prompt = _candidate_prompt(question, case_state, current_candidate=current_candidate)
    errors = []
    try:
        response = structured_chat(
            role="supervisor",
            messages=[{"role": "user", "content": prompt}],
            schema=CANDIDATE_ASSESSMENT_SCHEMA,
            metrics=metrics,
            purpose="candidate_assessment",
            think=False,
        )
        ok, obj, errors = parse_model_object(_message_text(response), CANDIDATE_ASSESSMENT_SCHEMA)
        if ok:
            result = normalise_candidate_assessment(obj, case_state)
            errors = candidate_errors(obj, case_state)
            if not errors:
                return result
            metrics["candidate_semantic_rejections"] = metrics.get("candidate_semantic_rejections", 0) + 1
            return {**result, "status": "AMBIGUOUS", "candidate": None, "reason": "; ".join(errors), "semantic_errors": errors}
        else:
            metrics["structured_output_rejections"] = metrics.get("structured_output_rejections", 0) + 1
    except Exception as exc:
        errors = [f"{type(exc).__name__}: {exc}"]

    if reasoner is not None and reasoner.available:
        remote = reasoner.fast(prompt, metrics=metrics, purpose="candidate_assessment", max_output_tokens=500, effort="low")
        if remote:
            ok, obj, errors = parse_model_object(remote, CANDIDATE_ASSESSMENT_SCHEMA)
            if ok:
                result = normalise_candidate_assessment(obj, case_state, source="remote_candidate_assessment")
                errors = candidate_errors(obj, case_state)
                if not errors:
                    return result

    metrics["candidate_assessment_failures"] = metrics.get("candidate_assessment_failures", 0) + 1
    if errors:
        print("[Candidate assessment unavailable: " + clip_text("; ".join(errors), 400) + "]")
    return None
