from __future__ import annotations

import json

from commander_agent.config import MAX_SEMANTIC_RECOVERY_STEPS
from commander_agent.core.reference_investigation import run_reference_investigation, verify_assessment
from commander_agent.core.executor import execute_splunk_search
from commander_agent.core.temporal_analysis import run_temporal_analysis
from commander_agent.core.measurement_resolution import resolve_measurement_semantics
from commander_agent.skills.result_semantics_recovery.scripts.capabilities import review_capability, assess_candidate
from commander_agent.skills.investigation_supervisor.scripts.reviewer import review_investigation_progress
from commander_agent.skills.scope_invariant_guard.scripts.reviewer import review_scope_change
from commander_agent.state.evidence import (
    current_evidence_version,
    quarantine_candidate,
    record_semantic_finding,
    record_semantic_finding_status,
    record_semantic_review,
    record_structured_finding,
)
from commander_agent.state.io import stable_hash
from commander_agent.state.scope import scope_violations, detectable_scope_change
from commander_agent.state.evidence_store import active_measurement_contract, measurement_contract_violations
from commander_agent.semantic.binding import semantic_binding_query_violations
from commander_agent.state.semantic_state import active_bindings
from commander_agent.skills.contracts import semantic_query_shape
from commander_agent.state.orchestration import (
    available_recovery_capabilities,
    can_stop_unresolved,
    ensure_orchestration_state,
    record_capability_attempt,
    set_stage,
)


QUERY_DECISIONS = {
    "RUN_CHECK_QUERY",
    "RUN_ALTERNATIVE_QUERY",
    "EXPAND_SCOPE",
    "INVESTIGATE_SCHEMA",
}

SUPPORTED_TERMINAL_DECISIONS = {"ACCEPT", "REINTERPRET"}


def semantic_review_feedback(review):
    if not review:
        return ""
    lines = [
        "INVESTIGATOR REVIEW:",
        f"Decision: {review.get('decision', '')}",
        f"Capability: {review.get('capability', '')}",
        f"Candidate status: {review.get('candidate_status', '')}",
        f"Evidence version: {review.get('evidence_version', '')}",
        f"Reason: {review.get('reason', '')}",
    ]
    if review.get("requested_quantity"):
        lines.append("Requested quantity: " + str(review["requested_quantity"]))
    if review.get("unsupported_claims"):
        lines.append("Unsupported claims: " + "; ".join(map(str, review["unsupported_claims"])))
    if review.get("supported_facts"):
        lines.append("Supported facts: " + "; ".join(map(str, review["supported_facts"])))
    if review.get("next_action"):
        lines.append("Next action: " + str(review["next_action"]))
    if review.get("candidate"):
        lines.append("Investigator candidate: " + str(review["candidate"]))
    if review.get("orchestration_status"):
        lines.append("Orchestration status: " + str(review["orchestration_status"]))
    lines.append("A separate Supervisor decides whether this action advanced the goal and what remains eligible.")
    return "\n".join(lines)


def _existing_support_ids(case_state, requested):
    known = {
        item.get("evidence_id")
        for item in case_state.get("evidence", [])
        if item.get("source") not in {"skill_reasoning", "deterministic"}
    }
    return [x for x in (requested or []) if x in known]


def _mark_finding(case_state, finding_state, status, note=""):
    if finding_state:
        record_semantic_finding_status(
            case_state,
            finding_state.get("finding_id"),
            status,
            note,
        )


def _query_action_signature(case_state, review):
    return stable_hash(
        {
            "evidence_version": current_evidence_version(case_state),
            "decision": review.get("decision"),
            "capability": review.get("capability"),
            "recommended_spl": " ".join(str(review.get("recommended_spl") or "").split()).lower(),
        }
    )


def _query_reuses_prior_evidence(case_state, query):
    shape = semantic_query_shape(query)
    if not shape:
        return None
    for item in case_state.get("evidence", []):
        prior = str(item.get("query") or "").strip()
        if prior and semantic_query_shape(prior) == shape:
            return item
    return None


def _candidate_gate_from_assessment(assessment):
    if not assessment or assessment.get("status") != "SUPPORTED" or not assessment.get("candidate"):
        return None
    return {
        "calculation_verified": assessment.get("calculation_verified", False),
        "ranking_evidence_id": assessment.get("ranking_evidence_id"),
        "decision": "ACCEPT",
        "capability": "candidate_validation",
        "candidate_status": "supported",
        "reason": assessment.get("reason", "Candidate assessment is supported by machine evidence."),
        "candidate": assessment.get("candidate"),
        "recommended_spl": None,
        "support_ids": list(assessment.get("support_ids") or []),
        "confidence": assessment.get("confidence", "medium"),
        "requested_quantity": assessment.get("requested_quantity", ""),
        "observed_semantics": {},
        "unsupported_claims": [],
        "supported_facts": [],
        "next_action": "validate candidate",
        "scope_change": {"requested": False, "reason": None},
        "reasoning_source": assessment.get("reasoning_source", "candidate_assessment"),
        "evidence_version": assessment.get("evidence_version"),
        "orchestration_status": "supported_supervisor_released_to_validation",
    }


def _print_supervisor(supervisor, label="SUPERVISOR EVALUATION"):
    print("\n" + "=" * 70)
    print(label)
    print("=" * 70)
    print(json.dumps(supervisor, ensure_ascii=False, indent=2))


def _supervise(
    *,
    question,
    case_state,
    metrics,
    last_action,
    last_result=None,
    candidate="",
    validation_feedback=None,
    reasoner=None,
):
    supervisor = review_investigation_progress(
        question=question,
        case_state=case_state,
        metrics=metrics,
        last_action=last_action,
        last_result=last_result,
        candidate=candidate,
        validation_feedback=validation_feedback,
        reasoner=reasoner,
    )
    _print_supervisor(supervisor)
    return supervisor


async def run_semantic_recovery(
    session,
    question,
    query_strategy,
    case_state,
    metrics,
    active_skills,
    reasoner=None,
    candidate="",
    validation_feedback=None,
):
    """Dynamic two-agent recovery loop with deterministic failover.

    Supervisor Agent: evaluates the current gap and orders the useful capabilities.
    Investigator Agent: receives exactly one assigned recovery capability at a time.
    Python: owns state transitions, scope/invariant enforcement, loop prevention,
    evidence versions, and failover. A child-skill failure is never treated as an
    investigation-wide failure while another eligible route remains.
    """
    if "investigation_supervisor" not in active_skills:
        active_skills.append("investigation_supervisor")
        case_state["active_skills"] = list(active_skills)

    state = ensure_orchestration_state(case_state)
    set_stage(case_state, "RECOVERY", goal=state.get("current_goal") or "Resolve the current evidence gap.")

    await run_reference_investigation(session, question, query_strategy, case_state, metrics, active_skills)
    if case_state.get("fatal_tool_error"):
        return {"decision": "STOP_ERROR", "reason": "Reference investigation encountered a fatal tool error."}
    resolve_measurement_semantics(question=question, case_state=case_state, metrics=metrics)
    prior_review = (case_state.get("semantic_reviews") or [None])[-1]
    last_review = None

    # ALR legacy compatibility separates candidate interpretation from recovery planning. The candidate
    # assessor has one tiny contract and cannot write SPL or choose a recovery route.
    initial_assessment = assess_candidate(
        question=question,
        case_state=case_state,
        metrics=metrics,
        current_candidate=candidate,
        reasoner=reasoner,
    )
    initial_assessment = await verify_assessment(session, initial_assessment, question, case_state, metrics, active_skills)
    if initial_assessment:
        print("\n" + "=" * 70)
        print("CANDIDATE ASSESSMENT")
        print("=" * 70)
        print(json.dumps(initial_assessment, ensure_ascii=False, indent=2))
    assessed_candidate = str((initial_assessment or {}).get("candidate") or "").strip()
    if assessed_candidate:
        candidate = assessed_candidate

    supervisor_feedback = _supervise(
        question=question,
        case_state=case_state,
        metrics=metrics,
        last_action={"decision": "CANDIDATE_ASSESSMENT", "capability": "candidate_validation"},
        last_result={
            "status": (initial_assessment or {}).get("status", "unavailable"),
            "evidence_version": current_evidence_version(case_state),
            "support_ids": (initial_assessment or {}).get("support_ids", []),
            "validation_feedback": validation_feedback or {},
        },
        candidate=candidate if assessed_candidate else "",
        validation_feedback=validation_feedback,
        reasoner=reasoner,
    )
    if assessed_candidate and supervisor_feedback.get("allow_validation"):
        gate = _candidate_gate_from_assessment(initial_assessment)
        if gate:
            metrics["semantic_skill_accepts"] = metrics.get("semantic_skill_accepts", 0) + 1
            return gate

    for step in range(1, MAX_SEMANTIC_RECOVERY_STEPS + 1):
        eligible = available_recovery_capabilities(
            case_state,
            (supervisor_feedback or {}).get("next_capabilities") if supervisor_feedback else None,
        )
        metrics["supervisor_eligible_snapshots"] = metrics.get("supervisor_eligible_snapshots", 0) + 1

        if not eligible:
            supervisor_feedback = _supervise(
                question=question,
                case_state=case_state,
                metrics=metrics,
                last_action={"decision": "ROUTES_EXHAUSTED", "capability": "controller"},
                last_result={"status": "no_eligible_recovery_capabilities"},
                candidate=candidate,
                validation_feedback=validation_feedback,
                reasoner=reasoner,
            )
            if can_stop_unresolved(case_state):
                return {
                    "decision": "STOP_UNRESOLVED",
                    "capability": "stop",
                    "candidate_status": "ambiguous" if not candidate else "not_evaluated",
                    "reason": supervisor_feedback.get("reason") or "No applicable recovery capability remains for the current unresolved goal.",
                    "candidate": None,
                    "recommended_spl": None,
                    "support_ids": [],
                    "orchestration_status": "semantic_unresolved_routes_exhausted",
                    "evidence_version": current_evidence_version(case_state),
                }
            # Defensive: if state changed during supervision, continue and recompute.
            continue

        # The Supervisor orders the routes; Python assigns only the first currently
        # eligible route. This gives the Investigator a narrow, auditable task while
        # preserving dynamic routing and deterministic fallback ordering.
        assigned_capability = eligible[0]
        metrics["capability_assignments"] = metrics.get("capability_assignments", 0) + 1
        print(f"\n[Supervisor assigned capability: {assigned_capability}]")

        review = review_capability(
            capability=assigned_capability,
            question=question,
            case_state=case_state,
            metrics=metrics,
            supervisor_feedback=supervisor_feedback,
            current_candidate=candidate,
            reasoner=reasoner,
        )

        if not review:
            # Critical ALR legacy compatibility behaviour: an Investigator protocol/action failure only
            # exhausts the assigned child route for the current goal. It returns to
            # the Supervisor instead of terminating the whole investigation.
            version = current_evidence_version(case_state)
            record_capability_attempt(
                case_state,
                assigned_capability,
                "contract_failure",
                version,
                version,
                note="Investigator did not produce a valid action after bounded repair.",
            )
            metrics["investigator_contract_failures"] = metrics.get("investigator_contract_failures", 0) + 1
            metrics["capability_failovers"] = metrics.get("capability_failovers", 0) + 1
            supervisor_feedback = _supervise(
                question=question,
                case_state=case_state,
                metrics=metrics,
                last_action={"decision": "CAPABILITY_FAILED", "capability": assigned_capability},
                last_result={
                    "status": "investigator_contract_failure",
                    "failed_capability": assigned_capability,
                    "evidence_version": version,
                },
                candidate=candidate,
                validation_feedback=validation_feedback,
                reasoner=reasoner,
            )
            prior_review = {
                "decision": "DEFER_CAPABILITY",
                "capability": assigned_capability,
                "candidate_status": "ambiguous",
                "reason": "Assigned capability failed its action contract; Supervisor must choose another route.",
                "candidate": None,
                "recommended_spl": None,
                "support_ids": [],
                "supervisor_feedback": supervisor_feedback,
            }
            continue

        support_ids = _existing_support_ids(case_state, review.get("support_ids"))
        review["support_ids"] = support_ids
        record_semantic_review(case_state, review, support_ids=support_ids)
        finding_state = record_semantic_finding(
            case_state,
            review,
            support_ids=support_ids,
            status="pending_supervisor",
        )
        last_review = review
        prior_review = review

        print("\n" + "=" * 70)
        print(f"INVESTIGATOR AGENT - RECOVERY STEP {step}")
        print("=" * 70)
        print(json.dumps(review, ensure_ascii=False, indent=2))

        decision = review.get("decision")
        capability = str(review.get("capability") or assigned_capability)
        candidate_status = review.get("candidate_status")
        supported_candidate = str(review.get("candidate") or "").strip()

        if decision == "STOP_ERROR":
            review["orchestration_status"] = "stop_error"
            _mark_finding(case_state, finding_state, "stop_error", review.get("reason", ""))
            return review

        if decision == "SEMANTIC_FINDING":
            case_state.setdefault("analytical_findings", []).append({"verdict": review.get("observed_semantics"), "reason": review.get("reason"), "support_ids": support_ids})
            record_capability_attempt(case_state, assigned_capability, "analytical_progress", note=review.get("reason", ""))
            metrics["analytical_progress"] = metrics.get("analytical_progress", 0) + 1
            _mark_finding(case_state, finding_state, "analytical_progress", review.get("reason", ""))
            supervisor_feedback = _supervise(question=question, case_state=case_state, metrics=metrics,
                last_action=review, last_result={"status": "analytical_progress"}, candidate="", reasoner=reasoner)
            continue

        if decision == "DEFER_CAPABILITY":
            version = current_evidence_version(case_state)
            record_capability_attempt(
                case_state,
                assigned_capability,
                "missing_prerequisite" if review.get("defer_kind") == "missing_prerequisite" else "irrelevant",
                version,
                version,
                prerequisites=review.get("prerequisites"),
                note=review.get("reason", "Assigned capability could not resolve the current gap."),
            )
            metrics["capability_deferrals"] = metrics.get("capability_deferrals", 0) + 1
            metrics["capability_failovers"] = metrics.get("capability_failovers", 0) + 1
            review["orchestration_status"] = "capability_deferred_to_supervisor"
            _mark_finding(case_state, finding_state, "deferred", review.get("reason", ""))
            supervisor_feedback = _supervise(
                question=question,
                case_state=case_state,
                metrics=metrics,
                last_action=review,
                last_result={
                    "status": "capability_insufficient",
                    "failed_capability": assigned_capability,
                    "suggested_capabilities": review.get("suggested_capabilities") or [],
                },
                candidate=candidate,
                validation_feedback=validation_feedback,
                reasoner=reasoner,
            )
            prior_review = dict(review)
            prior_review["supervisor_feedback"] = supervisor_feedback
            continue

        if decision == "STOP_UNRESOLVED":
            supervisor_feedback = _supervise(
                question=question,
                case_state=case_state,
                metrics=metrics,
                last_action=review,
                last_result={"status": "stop_requested", "eligible_before_request": eligible},
                candidate=candidate,
                validation_feedback=validation_feedback,
                reasoner=reasoner,
            )
            if supervisor_feedback.get("allow_stop") and can_stop_unresolved(case_state):
                review["orchestration_status"] = "semantic_unresolved_supervisor_approved"
                _mark_finding(case_state, finding_state, "unresolved", review.get("reason", ""))
                return review

            # The Investigator declined its assigned route. Exhaust that route for the
            # current goal so the next iteration cannot repeat the same refusal loop.
            version = current_evidence_version(case_state)
            record_capability_attempt(
                case_state,
                assigned_capability,
                "insufficient",
                version,
                version,
                note="Supervisor denied STOP_UNRESOLVED while recovery routes remained.",
            )
            metrics["supervisor_redirects"] = metrics.get("supervisor_redirects", 0) + 1
            metrics["capability_failovers"] = metrics.get("capability_failovers", 0) + 1
            review["orchestration_status"] = "stop_denied_by_supervisor"
            _mark_finding(case_state, finding_state, "redirected", supervisor_feedback.get("reason", ""))
            prior_review = dict(review)
            prior_review["supervisor_feedback"] = supervisor_feedback
            continue

        if decision in SUPPORTED_TERMINAL_DECISIONS:
            supervisor_feedback = _supervise(
                question=question,
                case_state=case_state,
                metrics=metrics,
                last_action=review,
                last_result={"status": "candidate_proposed", "support_ids": support_ids},
                candidate=supported_candidate,
                validation_feedback=validation_feedback,
                reasoner=reasoner,
            )
            if not supervisor_feedback.get("allow_validation"):
                metrics["supervisor_redirects"] = metrics.get("supervisor_redirects", 0) + 1
                review["orchestration_status"] = "candidate_held_by_supervisor"
                _mark_finding(case_state, finding_state, "redirected", supervisor_feedback.get("reason", ""))
                prior_review = dict(review)
                prior_review["supervisor_feedback"] = supervisor_feedback
                continue

            if candidate_status == "supported" and supported_candidate:
                finding = {
                    "verified": True,
                    "candidate": supported_candidate,
                    "support_ids": support_ids,
                    "review_decision": decision,
                    "confidence": review.get("confidence", "medium"),
                    "requested_quantity": review.get("requested_quantity", ""),
                    "observed_semantics": review.get("observed_semantics", {}),
                    "supported_facts": review.get("supported_facts", []),
                    "reason": review.get("reason", ""),
                    "source_skill": "result_semantics_recovery",
                    "supervisor_evaluation": supervisor_feedback.get("evaluation"),
                    "evidence_version": current_evidence_version(case_state),
                }
                finding_record = record_structured_finding(
                    case_state,
                    finding,
                    support_ids=support_ids,
                    method_class="semantic_skill_resolution",
                    source="skill_reasoning",
                )
                if finding_record:
                    finding["finding_evidence_id"] = finding_record.get("evidence_id")
                    if case_state.get("structured_findings"):
                        case_state["structured_findings"][-1]["finding_evidence_id"] = finding_record.get("evidence_id")
                metrics["semantic_skill_accepts"] = metrics.get("semantic_skill_accepts", 0) + 1
                review["orchestration_status"] = "supported_supervisor_released_to_validation"
                _mark_finding(case_state, finding_state, "accepted", review.get("reason", ""))
                set_stage(case_state, "VALIDATE", goal="Validate the candidate with materially independent evidence.")
                return review

            review["orchestration_status"] = "unsupported_terminal"
            _mark_finding(case_state, finding_state, "rejected_contract", "Supported terminal decision did not contain a supported candidate.")
            continue

        if decision == "RUN_TEMPORAL_ANALYSIS":
            if "temporal_pattern_analysis" not in active_skills:
                active_skills.append("temporal_pattern_analysis")
                case_state["active_skills"] = list(active_skills)
            version_before = current_evidence_version(case_state)
            analysis, evidence = await run_temporal_analysis(
                session=session,
                question=question,
                case_state=case_state,
                metrics=metrics,
                active_skills=active_skills,
                request=review.get("analysis_request") or {},
            )
            print("\nTEMPORAL PATTERN ANALYSIS RESULT:")
            print(json.dumps(analysis, ensure_ascii=False, indent=2))
            if case_state.get("fatal_tool_error"):
                review["orchestration_status"] = "fatal_tool_error"
                _mark_finding(case_state, finding_state, "failed_fatal_tool", "Fatal tool error during temporal analysis.")
                return review
            version_after = current_evidence_version(case_state)
            outcome = "progress" if evidence and version_after > version_before else "no_new_evidence"
            record_capability_attempt(
                case_state,
                assigned_capability,
                outcome,
                version_before,
                version_after,
                note=str(analysis.get("reason", "")) if isinstance(analysis, dict) else "",
            )
            assessment = None
            assessed_candidate = ""
            if outcome == "progress":
                resolve_measurement_semantics(question=question, case_state=case_state, metrics=metrics)
                assessment = assess_candidate(
                    question=question, case_state=case_state, metrics=metrics,
                    current_candidate=candidate, reasoner=reasoner,
                )
                assessment = await verify_assessment(session, assessment, question, case_state, metrics, active_skills)
                assessed_candidate = str((assessment or {}).get("candidate") or "").strip()
                if assessed_candidate:
                    candidate = assessed_candidate
                if assessment:
                    print("\nCANDIDATE ASSESSMENT AFTER TEMPORAL ANALYSIS:")
                    print(json.dumps(assessment, ensure_ascii=False, indent=2))
            supervisor_feedback = _supervise(
                question=question,
                case_state=case_state,
                metrics=metrics,
                last_action=review,
                last_result={
                    "status": outcome,
                    "evidence_before": version_before,
                    "evidence_after": version_after,
                    "analysis_summary": analysis,
                    "candidate_assessment": assessment or {},
                },
                candidate=candidate if assessed_candidate else "",
                validation_feedback=validation_feedback,
                reasoner=reasoner,
            )
            if outcome == "progress":
                review["orchestration_status"] = "temporal_analysis_new_evidence"
                _mark_finding(case_state, finding_state, "executed_temporal_analysis", f"Evidence advanced v{version_before}->v{version_after}.")
                if assessed_candidate and supervisor_feedback.get("allow_validation"):
                    gate = _candidate_gate_from_assessment(assessment)
                    if gate:
                        metrics["semantic_skill_accepts"] = metrics.get("semantic_skill_accepts", 0) + 1
                        return gate
            else:
                review["orchestration_status"] = "temporal_analysis_no_new_evidence"
                metrics["semantic_stagnation_blocks"] = metrics.get("semantic_stagnation_blocks", 0) + 1
                metrics["capability_failovers"] = metrics.get("capability_failovers", 0) + 1
                _mark_finding(case_state, finding_state, "stalled_temporal_analysis", str(analysis.get("reason", "no new evidence")))
            prior_review = dict(review)
            prior_review["supervisor_feedback"] = supervisor_feedback
            continue

        if decision == "REJECT_CANDIDATE":
            if not str(candidate or "").strip():
                review["orchestration_status"] = "rejected_contract"
                metrics["semantic_stagnation_blocks"] = metrics.get("semantic_stagnation_blocks", 0) + 1
                _mark_finding(case_state, finding_state, "rejected_contract", "REJECT_CANDIDATE reached orchestration without a current candidate.")
                continue
            quarantine_candidate(case_state, candidate, review=review)
            metrics["candidate_quarantines"] = metrics.get("candidate_quarantines", 0) + 1
            record_capability_attempt(case_state, "candidate_validation", "failed", note=review.get("reason", ""))
            candidate = ""
            supervisor_feedback = _supervise(
                question=question,
                case_state=case_state,
                metrics=metrics,
                last_action=review,
                last_result={"status": "candidate_rejected"},
                candidate="",
                validation_feedback=validation_feedback,
                reasoner=reasoner,
            )
            review["orchestration_status"] = "candidate_quarantined"
            _mark_finding(case_state, finding_state, "rejected", review.get("reason", ""))
            prior_review = dict(review)
            prior_review["supervisor_feedback"] = supervisor_feedback
            continue

        if decision in QUERY_DECISIONS:
            recommended_spl = str(review.get("recommended_spl") or "").strip()
            if not recommended_spl:
                version = current_evidence_version(case_state)
                review["orchestration_status"] = "missing_recommended_spl"
                metrics["semantic_stagnation_blocks"] = metrics.get("semantic_stagnation_blocks", 0) + 1
                metrics["capability_failovers"] = metrics.get("capability_failovers", 0) + 1
                record_capability_attempt(case_state, assigned_capability, "contract_failure", version, version, note="Query-producing decision omitted SPL.")
                _mark_finding(case_state, finding_state, "blocked_missing_spl", "Query-producing semantic decision omitted recommended_spl.")
                supervisor_feedback = _supervise(
                    question=question, case_state=case_state, metrics=metrics,
                    last_action=review,
                    last_result={"status": "capability_contract_failure", "failed_capability": assigned_capability},
                    candidate=candidate, validation_feedback=validation_feedback, reasoner=reasoner,
                )
                continue

            prior_evidence = _query_reuses_prior_evidence(case_state, recommended_spl)
            if prior_evidence is not None:
                version = current_evidence_version(case_state)
                metrics["query_reuse_blocks"] = metrics.get("query_reuse_blocks", 0) + 1
                metrics["capability_failovers"] = metrics.get("capability_failovers", 0) + 1
                review["orchestration_status"] = "prior_query_reuse_blocked"
                record_capability_attempt(
                    case_state, assigned_capability, "no_new_evidence", version, version,
                    note=f"Proposed query is semantically equivalent to existing evidence {prior_evidence.get('evidence_id')}.",
                )
                _mark_finding(case_state, finding_state, "blocked_prior_query_reuse", "Recovery query would only reuse existing evidence.")
                supervisor_feedback = _supervise(
                    question=question, case_state=case_state, metrics=metrics,
                    last_action=review,
                    last_result={
                        "status": "prior_query_reuse_blocked",
                        "existing_evidence_id": prior_evidence.get("evidence_id"),
                        "failed_capability": assigned_capability,
                    },
                    candidate=candidate, validation_feedback=validation_feedback, reasoner=reasoner,
                )
                continue

            if assigned_capability == "scope_expansion" and not detectable_scope_change(
                recommended_spl, case_state.get("scope_contract")
            ):
                version = current_evidence_version(case_state)
                metrics["capability_action_mismatch_blocks"] = metrics.get("capability_action_mismatch_blocks", 0) + 1
                metrics["capability_failovers"] = metrics.get("capability_failovers", 0) + 1
                review["orchestration_status"] = "scope_expansion_action_query_mismatch"
                record_capability_attempt(
                    case_state, assigned_capability, "contract_failure", version, version,
                    note="Scope expansion was requested but the query retained the same searchable scope prefix.",
                )
                _mark_finding(case_state, finding_state, "blocked_action_query_mismatch", "Declared scope expansion did not actually alter search scope.")
                supervisor_feedback = _supervise(
                    question=question, case_state=case_state, metrics=metrics,
                    last_action=review,
                    last_result={"status": "action_query_mismatch", "failed_capability": assigned_capability},
                    candidate=candidate, validation_feedback=validation_feedback, reasoner=reasoner,
                )
                continue

            violations = scope_violations(recommended_spl, case_state.get("scope_contract"))
            scope_change = review.get("scope_change") or {"requested": False, "reason": None}
            allow_scope_change = False
            if violations or bool(scope_change.get("requested")):
                if not bool(scope_change.get("requested")):
                    metrics["scope_guard_blocks"] = metrics.get("scope_guard_blocks", 0) + 1
                    review["orchestration_status"] = "scope_invariant_blocked"
                    record_capability_attempt(case_state, assigned_capability, "blocked", note="; ".join(violations))
                    _mark_finding(case_state, finding_state, "blocked_scope_invariant", "; ".join(violations))
                    supervisor_feedback = _supervise(
                        question=question,
                        case_state=case_state,
                        metrics=metrics,
                        last_action=review,
                        last_result={"status": "scope_invariant_blocked", "violations": violations},
                        candidate=candidate,
                        validation_feedback=validation_feedback,
                        reasoner=reasoner,
                    )
                    prior_review = dict(review)
                    prior_review["scope_guard_feedback"] = {"decision": "DENY", "violations": violations}
                    prior_review["supervisor_feedback"] = supervisor_feedback
                    continue

                scope_review = review_scope_change(
                    question=question,
                    scope_contract=case_state.get("scope_contract") or {},
                    proposed_query=recommended_spl,
                    justification=scope_change.get("reason"),
                    metrics=metrics,
                )
                review["scope_guard_review"] = scope_review
                if scope_review.get("decision") != "ALLOW":
                    metrics["scope_guard_blocks"] = metrics.get("scope_guard_blocks", 0) + 1
                    review["orchestration_status"] = "scope_change_denied"
                    record_capability_attempt(case_state, assigned_capability, "blocked", note=scope_review.get("reason", ""))
                    _mark_finding(case_state, finding_state, "scope_change_denied", scope_review.get("reason", ""))
                    supervisor_feedback = _supervise(
                        question=question,
                        case_state=case_state,
                        metrics=metrics,
                        last_action=review,
                        last_result={"status": "scope_change_denied", "scope_review": scope_review},
                        candidate=candidate,
                        validation_feedback=validation_feedback,
                        reasoner=reasoner,
                    )
                    prior_review = dict(review)
                    prior_review["scope_guard_feedback"] = scope_review
                    prior_review["supervisor_feedback"] = supervisor_feedback
                    continue
                review["orchestration_status"] = "scope_change_allowed"
                allow_scope_change = True
                _mark_finding(case_state, finding_state, "scope_change_allowed", scope_review.get("reason", ""))

            binding_violations = semantic_binding_query_violations(recommended_spl, active_bindings(case_state))
            if binding_violations:
                review["orchestration_status"] = "semantic_binding_blocked"
                metrics["semantic_binding_query_blocks"] = metrics.get("semantic_binding_query_blocks", 0) + 1
                record_capability_attempt(case_state, assigned_capability, "blocked", note="; ".join(binding_violations))
                _mark_finding(case_state, finding_state, "semantic_binding_blocked", "; ".join(binding_violations))
                supervisor_feedback = _supervise(question=question, case_state=case_state, metrics=metrics, last_action=review, last_result={"status":"semantic_binding_blocked","violations":binding_violations}, candidate=candidate, validation_feedback=validation_feedback, reasoner=reasoner)
                continue

            contract_violations = measurement_contract_violations(recommended_spl, active_measurement_contract(case_state))
            if contract_violations:
                review["orchestration_status"] = "measurement_contract_blocked"
                metrics["measurement_drift_blocks"] = metrics.get("measurement_drift_blocks", 0) + 1
                record_capability_attempt(case_state, assigned_capability, "blocked", note="; ".join(contract_violations))
                _mark_finding(case_state, finding_state, "measurement_contract_blocked", "; ".join(contract_violations))
                supervisor_feedback = _supervise(question=question, case_state=case_state, metrics=metrics, last_action=review, last_result={"status":"measurement_contract_blocked","violations":contract_violations}, candidate=candidate, validation_feedback=validation_feedback, reasoner=reasoner)
                continue
            action_signature = _query_action_signature(case_state, review)
            action_signatures = case_state.setdefault("semantic_action_signatures", set())
            if action_signature in action_signatures:
                review["orchestration_status"] = "repeat_action_blocked"
                metrics["semantic_stagnation_blocks"] = metrics.get("semantic_stagnation_blocks", 0) + 1
                metrics["capability_failovers"] = metrics.get("capability_failovers", 0) + 1
                record_capability_attempt(case_state, assigned_capability, "no_new_evidence", note="Same action already attempted for this evidence/goal state.", action_signature=action_signature)
                _mark_finding(case_state, finding_state, "blocked_repeat_action", "The same semantic query action was already attempted for this state.")
                supervisor_feedback = _supervise(
                    question=question,
                    case_state=case_state,
                    metrics=metrics,
                    last_action=review,
                    last_result={"status": "repeat_action_blocked"},
                    candidate=candidate,
                    validation_feedback=validation_feedback,
                    reasoner=reasoner,
                )
                continue
            action_signatures.add(action_signature)

            metrics["semantic_skill_query_actions"] = metrics.get("semantic_skill_query_actions", 0) + 1
            version_before = current_evidence_version(case_state)
            model_result, evidence, _ = await execute_splunk_search(
                session=session,
                tool_name="search_oneshot",
                arguments={
                    "query": recommended_spl,
                    "earliest_time": "0",
                    "latest_time": "now",
                    "max_count": 50,
                    "output_format": "json",
                },
                case_state=case_state,
                metrics=metrics,
                round_number=0,
                active_skills=active_skills,
                phase="CHECK",
                question=question,
                allow_scope_change=allow_scope_change,
            )
            print("\nINVESTIGATOR QUERY RESULT:")
            print(model_result)

            if case_state.get("fatal_tool_error"):
                review["orchestration_status"] = "fatal_tool_error"
                _mark_finding(case_state, finding_state, "failed_fatal_tool", "Fatal tool error.")
                return review

            version_after = current_evidence_version(case_state)
            outcome = "progress" if evidence and version_after > version_before else "no_new_evidence"
            record_capability_attempt(
                case_state,
                assigned_capability,
                outcome,
                version_before,
                version_after,
                note=review.get("reason", ""),
                action_signature=action_signature,
            )
            assessment = None
            assessed_candidate = ""
            if outcome == "progress":
                resolve_measurement_semantics(question=question, case_state=case_state, metrics=metrics)
                assessment = assess_candidate(
                    question=question, case_state=case_state, metrics=metrics,
                    current_candidate=candidate, reasoner=reasoner,
                )
                assessment = await verify_assessment(session, assessment, question, case_state, metrics, active_skills)
                assessed_candidate = str((assessment or {}).get("candidate") or "").strip()
                if assessed_candidate:
                    candidate = assessed_candidate
                if assessment:
                    print("\nCANDIDATE ASSESSMENT AFTER QUERY:")
                    print(json.dumps(assessment, ensure_ascii=False, indent=2))

            supervisor_feedback = _supervise(
                question=question,
                case_state=case_state,
                metrics=metrics,
                last_action=review,
                last_result={
                    "status": outcome,
                    "evidence_before": version_before,
                    "evidence_after": version_after,
                    "evidence_id": evidence.get("evidence_id") if evidence else None,
                    "candidate_assessment": assessment or {},
                },
                candidate=candidate if assessed_candidate else "",
                validation_feedback=validation_feedback,
                reasoner=reasoner,
            )

            if outcome == "progress":
                review["orchestration_status"] = "query_executed_new_evidence"
                _mark_finding(case_state, finding_state, "executed", f"Data evidence advanced v{version_before}->v{version_after}.")
                if assessed_candidate and supervisor_feedback.get("allow_validation"):
                    gate = _candidate_gate_from_assessment(assessment)
                    if gate:
                        metrics["semantic_skill_accepts"] = metrics.get("semantic_skill_accepts", 0) + 1
                        return gate
            else:
                review["orchestration_status"] = "stalled_no_new_evidence"
                metrics["semantic_stagnation_blocks"] = metrics.get("semantic_stagnation_blocks", 0) + 1
                metrics["capability_failovers"] = metrics.get("capability_failovers", 0) + 1
                _mark_finding(case_state, finding_state, "stalled_no_new_evidence", "The requested action produced no new data evidence.")
            prior_review = dict(review)
            prior_review["supervisor_feedback"] = supervisor_feedback
            continue

        # Unknown/inapplicable decision: fail only this capability and return to the
        # Supervisor. This closes the ALR bug where one malformed child action
        # terminated the whole investigation.
        version = current_evidence_version(case_state)
        review["orchestration_status"] = "unhandled_decision"
        metrics["semantic_stagnation_blocks"] = metrics.get("semantic_stagnation_blocks", 0) + 1
        metrics["capability_failovers"] = metrics.get("capability_failovers", 0) + 1
        record_capability_attempt(case_state, assigned_capability, "failed", version, version, note=str(decision))
        _mark_finding(case_state, finding_state, "unhandled_decision", str(decision))
        supervisor_feedback = _supervise(
            question=question,
            case_state=case_state,
            metrics=metrics,
            last_action=review,
            last_result={"status": "unhandled_decision", "failed_capability": assigned_capability},
            candidate=candidate,
            validation_feedback=validation_feedback,
            reasoner=reasoner,
        )

    metrics["recovery_budget_exhausted"] = metrics.get("recovery_budget_exhausted", 0) + 1
    return {
        "decision": "STOP_UNRESOLVED",
        "capability": "stop",
        "candidate_status": "ambiguous" if not candidate else "not_evaluated",
        "reason": "The bounded recovery budget was exhausted before validation. Remaining routes were not treated as proof of an answer.",
        "candidate": None,
        "recommended_spl": None,
        "support_ids": list((last_review or {}).get("support_ids") or []),
        "orchestration_status": "recovery_budget_exhausted",
        "evidence_version": current_evidence_version(case_state),
    }
