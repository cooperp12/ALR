from __future__ import annotations

from commander_agent.core.bounded_state import compact_control, short
import json
from pathlib import Path

from commander_agent.reasoning.local_ollama import structured_chat, message_content
from commander_agent.mcp.results import clip_text
from commander_agent.skills.structured_output_validation.scripts.schema import parse_model_object
from commander_agent.state.evidence import current_evidence_version
from commander_agent.state.orchestration import (
    ALL_CAPABILITIES,
    RECOVERY_CAPABILITIES,
    SAME_SCOPE_CAPABILITIES,
    available_recovery_capabilities,
    can_stop_unresolved,
    ensure_orchestration_state,
    record_supervisor_evaluation,
    supervisor_context,
)
from commander_agent.state.scope import render_scope_contract

from commander_agent.state.release import canonical_release_status


SUPERVISOR_SCHEMA = {
    "type": "object",
    "required": [
        "evaluation",
        "goal_satisfied",
        "remaining_gap",
        "next_capabilities",
        "irrelevant_capabilities",
        "invalidate_from_stage",
        "allow_validation",
        "allow_stop",
        "reason",
    ],
    "properties": {
        "evaluation": {
            "type": "string",
            "enum": ["PASS", "PARTIAL", "FAIL", "CONFLICT", "BLOCKED"],
        },
        "goal_satisfied": {"type": "boolean"},
        "remaining_gap": {"type": "string", "maxLength": 360},
        "next_capabilities": {
            "type": "array",
            "items": {"type": "string", "enum": RECOVERY_CAPABILITIES},
        },
        "irrelevant_capabilities": {
            "type": "array",
            "items": {"type": "string", "enum": RECOVERY_CAPABILITIES},
        },
        "invalidate_from_stage": {
            "type": ["string", "null"],
            "enum": [None, "UNDERSTAND", "CLASSIFY", "PRIMARY_ANALYSIS", "RECOVERY", "VALIDATE"],
        },
        "allow_validation": {"type": "boolean"},
        "allow_stop": {"type": "boolean"},
        "reason": {"type": "string", "maxLength": 360},
    },
}


def _skill_text():
    p = Path(__file__).resolve().parents[1] / "SKILL.md"
    text = p.read_text(encoding="utf-8")
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            text = parts[2]
    return text.strip()


def _machine_evidence_summary(case_state):
    rows = []
    for record in case_state.get("evidence", [])[-8:]:
        if record.get("source") in {"skill_reasoning"}:
            continue
        rows.append(
            {
                "evidence_id": record.get("evidence_id"),
                "evidence_version": record.get("evidence_version"),
                "source": record.get("source"),
                "method_class": record.get("method_class"),
                "event_count": record.get("event_count"),
                "query": clip_text(record.get("query", ""), 650),
            }
        )
    return rows


def _prompt(question, case_state, last_action=None, last_result=None, candidate="", validation_feedback=None):
    state = ensure_orchestration_state(case_state)
    available = available_recovery_capabilities(case_state)
    return f"""
You are the INVESTIGATION SUPERVISOR agent. You do NOT solve the cybersecurity question.
You monitor a separate Investigator agent and decide whether its latest action advanced
its current goal, what gap remains, whether an earlier stage is invalidated, and which
recovery capabilities should be eligible next.

Return exactly one JSON object matching the supplied schema.

Hard role separation:
- Do not output a candidate answer.
- Do not write SPL.
- Do not reinterpret individual event values to choose a winner.
- Evaluate progress and route selection only.
- Preserve the current scope contract unless a later independently reviewed scope change is necessary.
- Prefer useful same-scope capabilities before scope_expansion.
- Do not require every capability to run; mark genuinely irrelevant capabilities in irrelevant_capabilities.
- Do not allow STOP merely because one path failed or was blocked.
- allow_stop=true only when no applicable evidence-producing route remains.
- allow_validation=true only when the Investigator has a supported candidate and the current goal is sufficiently satisfied to send it to the independent validator.
- If new evidence conflicts with an earlier assumption, use invalidate_from_stage to backtrack only to the earliest affected stage.

=== SUPERVISOR SKILL ===
Keep remaining_gap and reason to one sentence each. Route schema inspection before alternative measurement when fields are unknown. Time analysis requires a time-related uncertainty. A missing prerequisite is not exhausted work.

=== USER QUESTION ===
{question}

=== CURRENT SCOPE CONTRACT ===
{render_scope_contract(case_state.get('scope_contract'))}

=== ORCHESTRATION STATE ===
{json.dumps(supervisor_context(case_state), ensure_ascii=False, indent=2)}

=== CURRENT AVAILABLE RECOVERY CAPABILITIES ===
{json.dumps(available, ensure_ascii=False)}

=== LAST INVESTIGATOR ACTION ===
{json.dumps(compact_control(last_action or {}), ensure_ascii=False)}

=== LAST ACTION RESULT ===
{json.dumps(compact_control(last_result or {}), ensure_ascii=False)}

=== CURRENT CANDIDATE PRESENCE ===
{"present" if str(candidate or '').strip() else "none"}

=== CANONICAL RELEASE STATE ===
{json.dumps(canonical_release_status(case_state, candidate=candidate or None), ensure_ascii=False, indent=2)}

=== VALIDATION FEEDBACK ===
{json.dumps(compact_control(validation_feedback or {}), ensure_ascii=False)}

=== MACHINE EVIDENCE METADATA ===
{json.dumps(_machine_evidence_summary(case_state), ensure_ascii=False, indent=2)}
""".strip()


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


def _normalise(obj, case_state):
    obj = dict(obj)
    obj["remaining_gap"] = short(obj.get("remaining_gap"))
    obj["reason"] = short(obj.get("reason"))
    valid = set(RECOVERY_CAPABILITIES)
    obj["next_capabilities"] = [x for x in obj.get("next_capabilities", []) if x in valid]
    obj["irrelevant_capabilities"] = [x for x in obj.get("irrelevant_capabilities", []) if x in valid]
    obj["evidence_version"] = current_evidence_version(case_state)
    return obj


def _deterministic_guard(obj, case_state, candidate=""):
    """Mechanically prevent premature stop/validation; never decide the domain answer."""
    obj = _normalise(obj, case_state)
    state = ensure_orchestration_state(case_state)

    # Apply only supervisor-declared irrelevance before computing remaining routes.
    declared_irrelevant = set(obj.get("irrelevant_capabilities") or [])
    version = str(current_evidence_version(case_state))
    existing_irrelevant = set((state.get("irrelevant_by_version") or {}).get(version, []))
    excluded = declared_irrelevant | existing_irrelevant
    remaining_same_scope = [
        x for x in available_recovery_capabilities(case_state)
        if x in SAME_SCOPE_CAPABILITIES and x not in excluded
    ]

    if obj.get("allow_stop") and remaining_same_scope:
        obj["allow_stop"] = False
        if obj.get("evaluation") == "PASS":
            obj["evaluation"] = "PARTIAL"
        obj["reason"] = (
            str(obj.get("reason") or "")
            + " Supervisor stop permission was mechanically denied because applicable same-scope routes remain: "
            + ", ".join(remaining_same_scope)
        ).strip()

    if obj.get("allow_validation") and not str(candidate or "").strip():
        obj["allow_validation"] = False
        obj["reason"] = (str(obj.get("reason") or "") + " Validation permission requires a current candidate.").strip()

    # ALR legacy compatibility canonical-release invariant: once durable Python state proves a complete
    # contract-derived unique maximum and persists its independent calculation check,
    # a weaker later recovery/query opinion cannot downgrade it. Only contract
    # invalidation or an open evidence/semantic conflict can close this gate.
    release = canonical_release_status(case_state, candidate=candidate or None)
    if release.get("ready") and str(candidate or "").strip() == str(release.get("candidate") or "").strip():
        obj["evaluation"] = "PASS"
        obj["goal_satisfied"] = True
        obj["remaining_gap"] = ""
        obj["next_capabilities"] = []
        obj["allow_validation"] = True
        obj["allow_stop"] = False
        obj["canonical_release_ready"] = True
        if release.get("contract_kind") == "extraction":
            obj["reason"] = (
                "Python canonical release gate is satisfied: active extraction contract, deterministic extracted candidate, "
                "persisted independent verification, and no open conflict."
            )
        else:
            obj["reason"] = (
                "Python canonical release gate is satisfied: complete active measurement contract, deterministic unique candidate, "
                "persisted independent calculation verification, and no open conflict."
            )

    available = available_recovery_capabilities(case_state, obj.get("next_capabilities"))
    obj["next_capabilities"] = [x for x in obj.get("next_capabilities", []) if x in available]
    if not obj["next_capabilities"] and not obj.get("allow_validation") and not obj.get("allow_stop"):
        obj["next_capabilities"] = available[:]
    return obj


def review_investigation_progress(
    question,
    case_state,
    metrics,
    last_action=None,
    last_result=None,
    candidate="",
    validation_feedback=None,
    reasoner=None,
):
    metrics["supervisor_reviews"] = metrics.get("supervisor_reviews", 0) + 1
    prompt = _prompt(
        question,
        case_state,
        last_action=last_action,
        last_result=last_result,
        candidate=candidate,
        validation_feedback=validation_feedback,
    )

    obj = None
    errors = []
    try:
        response = structured_chat(
            role="supervisor",
            messages=[{"role": "user", "content": prompt}],
            schema=SUPERVISOR_SCHEMA,
            metrics=metrics,
            purpose="investigation_supervisor",
            think=False,
        )
        ok, parsed, errors = parse_model_object(_message_text(response), SUPERVISOR_SCHEMA)
        if ok:
            obj = parsed
        else:
            metrics["supervisor_failures"] = metrics.get("supervisor_failures", 0) + 1
            metrics["structured_output_rejections"] = metrics.get("structured_output_rejections", 0) + 1
            print("[Supervisor JSON rejected: " + "; ".join(errors[:3]) + "]")
    except Exception as exc:
        metrics["supervisor_failures"] = metrics.get("supervisor_failures", 0) + 1
        errors = [f"{type(exc).__name__}: {exc}"]
        print(f"[Supervisor warning: {type(exc).__name__}: {exc}]")

    if obj is None and reasoner is not None and reasoner.available:
        remote = reasoner.fast(prompt, metrics=metrics, purpose="investigation_supervisor", max_output_tokens=700, effort="low")
        if remote:
            ok, parsed, errors = parse_model_object(remote, SUPERVISOR_SCHEMA)
            if ok:
                obj = parsed

    if obj is None:
        # Fail-open for investigation continuation but fail-closed for validation/stop.
        available = available_recovery_capabilities(case_state)
        obj = {
            "evaluation": "PARTIAL" if available else "BLOCKED",
            "goal_satisfied": False,
            "remaining_gap": ensure_orchestration_state(case_state).get("remaining_gap", "Investigation gap remains unresolved."),
            "next_capabilities": available,
            "irrelevant_capabilities": [],
            "invalidate_from_stage": None,
            "allow_validation": False,
            "allow_stop": not bool(available),
            "reason": "Supervisor model was unavailable; deterministic orchestration preserved remaining routes.",
        }
        metrics["supervisor_fallbacks"] = metrics.get("supervisor_fallbacks", 0) + 1

    obj = _deterministic_guard(obj, case_state, candidate=candidate)
    record_supervisor_evaluation(case_state, obj)
    if obj.get("allow_stop"):
        metrics["supervisor_stop_allows"] = metrics.get("supervisor_stop_allows", 0) + 1
    else:
        metrics["supervisor_stop_denials"] = metrics.get("supervisor_stop_denials", 0) + 1
    if obj.get("allow_validation"):
        metrics["supervisor_validation_allows"] = metrics.get("supervisor_validation_allows", 0) + 1
    if obj.get("canonical_release_ready"):
        metrics["canonical_release_allows"] = metrics.get("canonical_release_allows", 0) + 1
    return obj
