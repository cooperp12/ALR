from __future__ import annotations

import json
from pathlib import Path

from commander_agent.config import (
    MODEL,
    MAX_SEMANTIC_FORMAT_REPAIRS,
    MAX_SEMANTIC_ACTION_REPAIRS,
    MAX_SEMANTIC_REVIEWS_PER_EVIDENCE_VERSION,
)
from commander_agent.mcp.results import clip_text
from commander_agent.state.evidence import (
    bump_semantic_review_budget,
    current_evidence_version,
    semantic_review_budget_available,
)
from commander_agent.skills.structured_output_validation.scripts.schema import parse_model_object
from commander_agent.state.scope import render_scope_contract
from commander_agent.state.orchestration import RECOVERY_CAPABILITIES


def _ollama_chat(**kwargs):
    import ollama
    return ollama.chat(**kwargs)


QUERY_DECISIONS = {
    "RUN_CHECK_QUERY",
    "RUN_ALTERNATIVE_QUERY",
    "EXPAND_SCOPE",
    "INVESTIGATE_SCHEMA",
}
SUPPORTED_TERMINAL_DECISIONS = {"ACCEPT", "REINTERPRET"}
TEMPORAL_DECISIONS = {"RUN_TEMPORAL_ANALYSIS"}

# Keep the mandatory protocol deliberately small. Rich diagnostics are optional so a
# useful semantic action is not discarded just because a small local model omitted
# explanatory metadata.
SEMANTIC_DECISION_SCHEMA = {
    "type": "object",
    "required": [
        "decision",
        "capability",
        "candidate_status",
        "reason",
        "candidate",
        "recommended_spl",
        "support_ids",
    ],
    "properties": {
        "capability": {
            "type": "string",
            "enum": RECOVERY_CAPABILITIES + ["candidate_validation", "stop", "error"],
        },
        "decision": {
            "type": "string",
            "enum": [
                "ACCEPT",
                "REINTERPRET",
                "RUN_CHECK_QUERY",
                "RUN_ALTERNATIVE_QUERY",
                "EXPAND_SCOPE",
                "INVESTIGATE_SCHEMA",
                "RUN_TEMPORAL_ANALYSIS",
                "DEFER_CAPABILITY",
                "REJECT_CANDIDATE",
                "STOP_UNRESOLVED",
                "STOP_ERROR",
            ],
        },
        "candidate_status": {
            "type": "string",
            "enum": ["supported", "unsupported", "ambiguous", "not_evaluated"],
        },
        "reason": {"type": "string"},
        "candidate": {"type": ["string", "null"]},
        "recommended_spl": {"type": ["string", "null"]},
        "support_ids": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "issue_type": {"type": "string"},
        "requested_quantity": {"type": "string"},
        "observed_semantics": {"type": "object"},
        "unsupported_claims": {"type": "array", "items": {"type": "string"}},
        "supported_facts": {"type": "array", "items": {"type": "string"}},
        "next_action": {"type": "string"},
        "suggested_capabilities": {
            "type": "array",
            "items": {"type": "string", "enum": RECOVERY_CAPABILITIES},
        },
        "analysis_request": {
            "type": "object",
            "required": ["group_field", "value_field", "time_field", "measures", "comparison_feature"],
            "properties": {
                "group_field": {"type": ["string", "null"]},
                "value_field": {"type": ["string", "null"]},
                "time_field": {"type": "string"},
                "measures": {
                    "type": "array",
                    "items": {"type": "string", "enum": [
                        "event_count", "distinct_count", "cumulative_distinct_count", "new_distinct_count"
                    ]}
                },
                "comparison_feature": {"type": ["string", "null"]}
            }
        },
        "scope_change": {
            "type": "object",
            "required": ["requested", "reason"],
            "properties": {
                "requested": {"type": "boolean"},
                "reason": {"type": ["string", "null"]}
            }
        },
    },
}

# Backward import name retained for tests/extensions.
SEMANTIC_RECOVERY_SCHEMA = SEMANTIC_DECISION_SCHEMA


def _skill_text():
    skill_path = Path(__file__).resolve().parents[1] / "SKILL.md"
    text = skill_path.read_text(encoding="utf-8")
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            text = parts[2]
    return text.strip()


def _temporal_skill_text():
    skill_path = Path(__file__).resolve().parents[2] / "temporal_pattern_analysis" / "SKILL.md"
    if not skill_path.exists():
        return "[not installed]"
    text = skill_path.read_text(encoding="utf-8")
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            text = parts[2]
    return text.strip()


def _data_evidence_digest(case_state, max_items=8, excerpt_chars=1800):
    """Return immutable machine data evidence, never prior free-form model prose."""
    records = [
        r
        for r in case_state.get("evidence", [])
        if r.get("source") not in {"skill_reasoning", "deterministic"}
    ][-max_items:]
    parts = []
    for record in records:
        parts.append(
            f"{record.get('evidence_id')} | version={record.get('evidence_version')} "
            f"| source={record.get('source')} | method={record.get('method_class')}\n"
            f"QUERY: {clip_text(record.get('query', ''), 900)}\n"
            f"RESULT: {clip_text(record.get('result_excerpt', ''), excerpt_chars)}"
        )
    return "\n\n".join(parts)


def _prompt(question, query_strategy, case_state, candidate="", prior_review=None, validation_feedback=None, supervisor_feedback=None, eligible_capabilities=None, assigned_capability=None):
    evidence = _data_evidence_digest(case_state)
    return f"""
You are executing the RESULT SEMANTICS AND RECOVERY skill below.
The skill, not Python, owns semantic interpretation and the recovery choice.
Use ONLY the current question, the machine-produced SPL/data evidence, the current
candidate (if any), and the immediately prior semantic decision.
Do not use lab/scorer answers, remembered challenge answers, or prior free-form agent prose.

IMPORTANT OUTPUT CONTRACT
- Return exactly one JSON object matching the supplied JSON schema.
- capability MUST identify the analytical capability used by this decision.
- For recovery actions, use the ASSIGNED CAPABILITY. Do not silently morph into a sibling capability.
- If the assigned capability cannot resolve the current gap or needs a different capability, return DEFER_CAPABILITY with suggested_capabilities instead of pretending to execute the sibling.
- The Supervisor controls routing; do not choose STOP_UNRESOLVED when it says useful routes remain.
- candidate MUST be a string or null.
- recommended_spl MUST be a string or null.
- support_ids MUST identify only evidence IDs actually shown below.
- Preserve the CURRENT SCOPE CONTRACT. Changing how a concept is measured is not a
  scope change. Removing/changing current search population constraints is a scope change.
- If more evidence is required and a useful query can resolve the ambiguity, choose a
  query-producing decision and put the exact executable SPL in recommended_spl.
- RUN_TEMPORAL_ANALYSIS delegates numeric/time-series computation to deterministic Python.
  Use it when trends, bursts, diversity growth, or entity behaviour over time can materially
  resolve the question. Supply analysis_request; do not estimate those numeric features yourself.
- EXPAND_SCOPE or any query that intentionally weakens a scope invariant must set
  scope_change.requested=true and explain why in scope_change.reason. That change will be
  independently reviewed before execution. Prefer preserving scope and changing measurement.
- REJECT_CANDIDATE is only valid when CURRENT CANDIDATE contains an actual candidate
  and the evidence makes that candidate unsupported. It is NOT a synonym for
  "evidence is ambiguous".
- If CURRENT CANDIDATE is [none] and evidence is ambiguous, either gather new evidence
  with RUN_CHECK_QUERY / RUN_ALTERNATIVE_QUERY / INVESTIGATE_SCHEMA / RUN_TEMPORAL_ANALYSIS / EXPAND_SCOPE, or
  use STOP_UNRESOLVED only when no useful evidence-gathering action remains.
- Do not say "run another query" without supplying that query.
- DEFER_CAPABILITY means the assigned capability is insufficient on the current evidence; it must not contain recommended_spl.

=== RESULT-SEMANTICS SKILL ===
{_skill_text()}

=== AVAILABLE TEMPORAL ANALYTICS SKILL ===
{_temporal_skill_text()}

=== QUESTION ===
{question}

=== CURRENT SCOPE CONTRACT ===
{render_scope_contract(case_state.get("scope_contract"))}

=== EVIDENCE VERSION ===
{current_evidence_version(case_state)}

=== CURRENT CANDIDATE ===
{candidate or "[none]"}

=== ASSIGNED CAPABILITY ===
{assigned_capability or "[none]"}

=== CURRENT ELIGIBLE CAPABILITIES ===
{json.dumps(list(eligible_capabilities or []), ensure_ascii=False)}

=== SUPERVISOR FEEDBACK ===
{json.dumps(supervisor_feedback or {}, ensure_ascii=False, indent=2) if supervisor_feedback else "[none]"}

=== QUERY STRATEGY ===
{json.dumps(query_strategy or {}, ensure_ascii=False, indent=2)}

=== PRIOR SEMANTIC DECISION ===
{json.dumps(prior_review or {}, ensure_ascii=False, indent=2) if prior_review else "[none]"}

=== INDEPENDENT VALIDATION FEEDBACK ===
{json.dumps(validation_feedback or {}, ensure_ascii=False, indent=2) if validation_feedback else "[none]"}

=== MACHINE DATA EVIDENCE ===
{evidence or "[none]"}
""".strip()


def _format_repair_prompt(original_text, errors):
    return f"""
Repair the JSON protocol only.

The previous semantic decision failed schema validation:
{chr(10).join('- ' + str(e) for e in errors[:8])}

Return ONE JSON object matching the supplied schema.
Preserve the previous decision, candidate, evidence IDs, reason, and recommended SPL
whenever they are already present. Fill missing protocol fields with valid values.
Do not perform a new investigation. Do not invent new evidence or facts.

PREVIOUS OUTPUT:
{original_text}
""".strip()


def _action_repair_prompt(base_prompt, previous_obj, errors):
    """Ask the skill to repair an internally inconsistent *action*, not the evidence."""
    return f"""
Your previous semantic decision was valid JSON but violates the generic action contract.

ACTION-CONTRACT ERRORS:
{chr(10).join('- ' + str(e) for e in errors[:8])}

Re-evaluate ONLY the action choice against the same evidence. Do not invent facts, do
not use any expected/lab answer, and do not change the machine evidence.

Generic action rules:
- ACCEPT/REINTERPRET require candidate_status=supported and a non-empty candidate.
- RUN_CHECK_QUERY/RUN_ALTERNATIVE_QUERY/INVESTIGATE_SCHEMA/EXPAND_SCOPE require executable recommended_spl.
- RUN_TEMPORAL_ANALYSIS requires a complete analysis_request; the harness constructs a scoped reducing SPL.
- DEFER_CAPABILITY keeps the currently assigned capability, has no SPL, and suggests another capability when this capability cannot resolve the gap.
- EXPAND_SCOPE requires scope_change.requested=true plus a non-empty justification, and will be independently scope-reviewed.
- REJECT_CANDIDATE requires an actual CURRENT CANDIDATE and candidate_status=unsupported.
- Ambiguous evidence with no current candidate must not use REJECT_CANDIDATE.
- If ambiguity can reasonably be resolved with another focused query or deterministic
  temporal analysis, choose the corresponding evidence-gathering action. Supply SPL for
  SPL actions and analysis_request for RUN_TEMPORAL_ANALYSIS.
- STOP_UNRESOLVED is for a genuine semantic dead end where no useful evidence action
  remains. STOP_ERROR is only for a non-recoverable tool/infrastructure condition.

Return exactly one JSON object matching the supplied schema.

PREVIOUS DECISION:
{json.dumps(previous_obj, ensure_ascii=False, indent=2)}

ORIGINAL SEMANTIC TASK AND EVIDENCE:
{base_prompt}
""".strip()


def _message_text(response):
    message = getattr(response, "message", None)
    if message is None and isinstance(response, dict):
        message = response.get("message")
    if isinstance(message, dict):
        return str(message.get("content") or "")
    return str(getattr(message, "content", "") or "")


def _add_usage(metrics, response):
    prompt_tokens = getattr(response, "prompt_eval_count", None)
    output_tokens = getattr(response, "eval_count", None)
    if isinstance(response, dict):
        prompt_tokens = response.get("prompt_eval_count", prompt_tokens)
        output_tokens = response.get("eval_count", output_tokens)
    if prompt_tokens is not None:
        metrics["ollama_prompt_tokens"] = metrics.get("ollama_prompt_tokens", 0) + int(
            prompt_tokens or 0
        )
    if output_tokens is not None:
        metrics["ollama_output_tokens"] = metrics.get("ollama_output_tokens", 0) + int(
            output_tokens or 0
        )


def _normalise_decision(obj, source, case_state):
    # Optional diagnostic keys receive mechanical defaults. This is protocol
    # normalisation only; it does not change the skill's semantic decision.
    obj = dict(obj)
    obj.setdefault("confidence", "medium")
    obj.setdefault("issue_type", "")
    obj.setdefault("requested_quantity", "")
    obj.setdefault("observed_semantics", {})
    obj.setdefault("unsupported_claims", [])
    obj.setdefault("supported_facts", [])
    obj.setdefault("next_action", "")
    obj.setdefault("suggested_capabilities", [])
    obj.setdefault("scope_change", {"requested": False, "reason": None})
    obj["candidate"] = (
        obj.get("candidate") if obj.get("candidate") not in ("", None) else None
    )
    obj["recommended_spl"] = (
        obj.get("recommended_spl")
        if obj.get("recommended_spl") not in ("", None)
        else None
    )
    obj["reasoning_source"] = source
    obj["evidence_version"] = current_evidence_version(case_state)
    return obj


def validate_action_contract(obj, current_candidate="", eligible_capabilities=None, assigned_capability=None):
    """Validate generic action consistency without interpreting domain evidence."""
    errors = []
    decision = str(obj.get("decision") or "")
    capability = str(obj.get("capability") or "")
    status = str(obj.get("candidate_status") or "")
    candidate = str(obj.get("candidate") or "").strip()
    current_candidate = str(current_candidate or "").strip()
    recommended_spl = str(obj.get("recommended_spl") or "").strip()
    analysis_request = obj.get("analysis_request")
    scope_change = obj.get("scope_change") or {"requested": False, "reason": None}

    decision_capability_rules = {
        "INVESTIGATE_SCHEMA": {"schema_inspection"},
        "RUN_TEMPORAL_ANALYSIS": {"temporal_pattern_analysis"},
        "EXPAND_SCOPE": {"scope_expansion"},
        "ACCEPT": {"candidate_validation"},
        "REINTERPRET": {"candidate_validation"},
        "REJECT_CANDIDATE": {"candidate_validation"},
        "STOP_UNRESOLVED": {"stop"},
        "STOP_ERROR": {"error"},
        "RUN_CHECK_QUERY": {"semantic_field_review", "alternative_aggregation", "relationship_analysis"},
        "RUN_ALTERNATIVE_QUERY": {"semantic_field_review", "alternative_aggregation", "relationship_analysis"},
    }
    allowed_for_decision = decision_capability_rules.get(decision, set())
    if allowed_for_decision and capability not in allowed_for_decision:
        errors.append(f"{decision} is incompatible with capability={capability}")
    if capability in RECOVERY_CAPABILITIES and eligible_capabilities is not None:
        if capability not in set(eligible_capabilities):
            errors.append(f"capability={capability} is not currently eligible")
    if assigned_capability and capability in RECOVERY_CAPABILITIES and capability != assigned_capability:
        errors.append(f"capability={capability} does not match assigned capability={assigned_capability}")

    if decision in QUERY_DECISIONS and not recommended_spl:
        errors.append(f"{decision} requires executable recommended_spl")

    if decision == "DEFER_CAPABILITY":
        if capability not in RECOVERY_CAPABILITIES:
            errors.append("DEFER_CAPABILITY requires the currently assigned recovery capability")
        if recommended_spl:
            errors.append("DEFER_CAPABILITY must not include recommended_spl")
        suggestions = obj.get("suggested_capabilities") or []
        if not isinstance(suggestions, list):
            errors.append("DEFER_CAPABILITY suggested_capabilities must be an array")
        elif any(x not in RECOVERY_CAPABILITIES for x in suggestions):
            errors.append("DEFER_CAPABILITY contains an unknown suggested capability")
        if candidate and status == "supported":
            errors.append("DEFER_CAPABILITY must not carry a supported candidate; use ACCEPT/REINTERPRET")

    if decision == "RUN_TEMPORAL_ANALYSIS":
        if not isinstance(analysis_request, dict):
            errors.append("RUN_TEMPORAL_ANALYSIS requires analysis_request")
        else:
            if not str(analysis_request.get("time_field") or "").strip():
                errors.append("RUN_TEMPORAL_ANALYSIS requires analysis_request.time_field")
            if not isinstance(analysis_request.get("measures"), list) or not analysis_request.get("measures"):
                errors.append("RUN_TEMPORAL_ANALYSIS requires at least one analysis_request.measures value")
        if recommended_spl:
            errors.append("RUN_TEMPORAL_ANALYSIS must not supply recommended_spl; the harness builds scoped SPL")

    if decision == "EXPAND_SCOPE":
        if not bool(scope_change.get("requested")):
            errors.append("EXPAND_SCOPE requires scope_change.requested=true")
        if not str(scope_change.get("reason") or "").strip():
            errors.append("EXPAND_SCOPE requires a non-empty scope_change.reason")

    if bool(scope_change.get("requested")) and not str(scope_change.get("reason") or "").strip():
        errors.append("scope_change.requested=true requires scope_change.reason")

    if decision in SUPPORTED_TERMINAL_DECISIONS:
        if status != "supported":
            errors.append(f"{decision} requires candidate_status=supported")
        if not candidate:
            errors.append(f"{decision} requires a non-empty candidate")

    if decision == "REJECT_CANDIDATE":
        if not current_candidate:
            errors.append("REJECT_CANDIDATE requires an actual CURRENT CANDIDATE")
        if status != "unsupported":
            errors.append("REJECT_CANDIDATE requires candidate_status=unsupported")
        if recommended_spl:
            errors.append(
                "REJECT_CANDIDATE must not carry recommended_spl; use a query-producing decision instead"
            )

    if status in {"ambiguous", "not_evaluated"} and decision in (
        SUPPORTED_TERMINAL_DECISIONS | {"REJECT_CANDIDATE"}
    ):
        errors.append(
            f"candidate_status={status} is incompatible with terminal decision {decision}"
        )

    if decision == "STOP_UNRESOLVED":
        if status == "supported":
            errors.append("STOP_UNRESOLVED cannot be used for a supported candidate")
        if recommended_spl:
            errors.append(
                "STOP_UNRESOLVED cannot include recommended_spl; use a query-producing decision"
            )

    if decision == "STOP_ERROR" and recommended_spl:
        errors.append("STOP_ERROR cannot include recommended_spl")

    return errors


def _parse_and_normalise(text, source, case_state):
    ok, obj, errors = parse_model_object(text, SEMANTIC_DECISION_SCHEMA)
    if not ok:
        return None, errors
    return _normalise_decision(obj, source, case_state), []


def _repair_action_if_needed(obj, base_prompt, current_candidate, case_state, metrics, eligible_capabilities=None, assigned_capability=None):
    errors = validate_action_contract(obj, current_candidate=current_candidate, eligible_capabilities=eligible_capabilities, assigned_capability=assigned_capability)
    if not errors:
        return obj

    metrics["semantic_action_contract_rejections"] = metrics.get(
        "semantic_action_contract_rejections", 0
    ) + 1
    print("[Semantic action contract rejected: " + "; ".join(errors[:3]) + "]")

    previous = obj
    for _ in range(MAX_SEMANTIC_ACTION_REPAIRS):
        metrics["semantic_action_repairs"] = metrics.get("semantic_action_repairs", 0) + 1
        try:
            response = _ollama_chat(
                model=MODEL,
                messages=[
                    {
                        "role": "user",
                        "content": _action_repair_prompt(base_prompt, previous, errors),
                    }
                ],
                format=SEMANTIC_DECISION_SCHEMA,
                options={"num_ctx": 8192, "temperature": 0},
                keep_alive="10m",
            )
            _add_usage(metrics, response)
            repaired, schema_errors = _parse_and_normalise(
                _message_text(response), "local_action_repair", case_state
            )
            if repaired is None:
                metrics["structured_output_rejections"] = metrics.get(
                    "structured_output_rejections", 0
                ) + 1
                errors = schema_errors
                print("[Semantic action repair JSON rejected: " + "; ".join(errors[:3]) + "]")
                continue
            errors = validate_action_contract(
                repaired, current_candidate=current_candidate, eligible_capabilities=eligible_capabilities, assigned_capability=assigned_capability
            )
            if not errors:
                metrics["semantic_action_repair_successes"] = metrics.get(
                    "semantic_action_repair_successes", 0
                ) + 1
                return repaired
            previous = repaired
            metrics["semantic_action_contract_rejections"] = metrics.get(
                "semantic_action_contract_rejections", 0
            ) + 1
            print("[Semantic action repair still inconsistent: " + "; ".join(errors[:3]) + "]")
        except Exception as exc:
            errors = [f"{type(exc).__name__}: {exc}"]
            print(f"[Semantic action repair warning: {type(exc).__name__}: {exc}]")

    return None


def review_result_semantics(
    question,
    query_strategy,
    case_state,
    metrics,
    candidate="",
    reasoner=None,
    prior_review=None,
    validation_feedback=None,
    supervisor_feedback=None,
    eligible_capabilities=None,
    assigned_capability=None,
):
    """Return one schema-valid and action-consistent semantic decision.

    Python validates the protocol and generic state/action consistency. It never selects
    a domain field, candidate, winner, or recovery SPL. When an action is inconsistent,
    the skill receives one bounded action-repair turn over the same evidence.
    """
    if not semantic_review_budget_available(
        case_state, MAX_SEMANTIC_REVIEWS_PER_EVIDENCE_VERSION
    ):
        metrics["semantic_stagnation_blocks"] = metrics.get(
            "semantic_stagnation_blocks", 0
        ) + 1
        print(
            "[Semantic review budget reached for evidence version "
            f"{current_evidence_version(case_state)}; new data evidence is required.]"
        )
        return None

    bump_semantic_review_budget(case_state)
    metrics["semantic_skill_reviews"] = metrics.get("semantic_skill_reviews", 0) + 1
    prompt = _prompt(
        question,
        query_strategy,
        case_state,
        candidate=candidate,
        prior_review=prior_review,
        validation_feedback=validation_feedback,
        supervisor_feedback=supervisor_feedback,
        eligible_capabilities=eligible_capabilities,
        assigned_capability=assigned_capability,
    )

    local_text = ""
    local_errors = []
    try:
        response = _ollama_chat(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            format=SEMANTIC_DECISION_SCHEMA,
            options={"num_ctx": 8192, "temperature": 0},
            keep_alive="10m",
        )
        _add_usage(metrics, response)
        local_text = _message_text(response)
        obj, local_errors = _parse_and_normalise(local_text, "local", case_state)
        if obj is not None:
            repaired = _repair_action_if_needed(
                obj, prompt, candidate, case_state, metrics, eligible_capabilities=eligible_capabilities, assigned_capability=assigned_capability
            )
            if repaired is not None:
                return repaired
            local_errors = ["semantic action remained internally inconsistent after bounded repair"]
        else:
            metrics["structured_output_rejections"] = metrics.get(
                "structured_output_rejections", 0
            ) + 1
            print("[Semantic skill JSON rejected: " + "; ".join(local_errors[:3]) + "]")
    except Exception as exc:
        local_errors = [f"{type(exc).__name__}: {exc}"]
        print(f"[Semantic skill local reasoning warning: {type(exc).__name__}: {exc}]")

    # Exactly one formatting-only repair by default. If it succeeds, the resulting
    # object still has to pass the generic action contract above.
    for _ in range(MAX_SEMANTIC_FORMAT_REPAIRS):
        if not local_text:
            break
        metrics["semantic_format_repairs"] = metrics.get("semantic_format_repairs", 0) + 1
        try:
            response = _ollama_chat(
                model=MODEL,
                messages=[
                    {
                        "role": "user",
                        "content": _format_repair_prompt(local_text, local_errors),
                    }
                ],
                format=SEMANTIC_DECISION_SCHEMA,
                options={"num_ctx": 4096, "temperature": 0},
                keep_alive="10m",
            )
            _add_usage(metrics, response)
            repaired_text = _message_text(response)
            obj, errors = _parse_and_normalise(
                repaired_text, "local_format_repair", case_state
            )
            if obj is not None:
                metrics["semantic_format_repair_successes"] = metrics.get(
                    "semantic_format_repair_successes", 0
                ) + 1
                action_repaired = _repair_action_if_needed(
                    obj, prompt, candidate, case_state, metrics, eligible_capabilities=eligible_capabilities, assigned_capability=assigned_capability
                )
                if action_repaired is not None:
                    return action_repaired
                local_errors = [
                    "semantic action remained internally inconsistent after bounded repair"
                ]
            else:
                local_errors = errors
                metrics["structured_output_rejections"] = metrics.get(
                    "structured_output_rejections", 0
                ) + 1
                print("[Semantic format repair JSON rejected: " + "; ".join(errors[:3]) + "]")
        except Exception as exc:
            local_errors = [f"{type(exc).__name__}: {exc}"]
            print(f"[Semantic format repair warning: {type(exc).__name__}: {exc}]")

    # Optional remote fallback remains disabled in the shipped configuration. It is
    # still held to the same schema and action contract.
    if reasoner is not None and reasoner.available:
        remote = reasoner.fast(
            prompt,
            metrics=metrics,
            purpose="semantic_recovery",
            max_output_tokens=1200,
            effort="low",
        )
        if remote:
            obj, errors = _parse_and_normalise(remote, "remote", case_state)
            if obj is not None:
                action_repaired = _repair_action_if_needed(
                    obj, prompt, candidate, case_state, metrics, eligible_capabilities=eligible_capabilities, assigned_capability=assigned_capability
                )
                if action_repaired is not None:
                    return action_repaired
            else:
                local_errors = errors
                metrics["structured_output_rejections"] = metrics.get(
                    "structured_output_rejections", 0
                ) + 1
                print("[Remote semantic JSON rejected: " + "; ".join(errors[:3]) + "]")

    metrics["semantic_skill_failures"] = metrics.get("semantic_skill_failures", 0) + 1
    if local_errors:
        print(
            "[Semantic skill unavailable after bounded repair: "
            + clip_text("; ".join(local_errors), 500)
            + "]"
        )
    return None
