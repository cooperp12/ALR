import asyncio
import json

from commander_agent.config import (
    OLLAMA_NUM_CTX,
    MAX_AGENT_ROUNDS,
    MAX_SEARCH_EXECUTIONS,
    MIN_VALIDATION_SUPPORT,
    MAX_TOTAL_MODEL_RESULT_CHARS,
    ENABLE_BOOTSTRAP_QUERIES,
    MAX_AGENT_TURNS_WITHOUT_NEW_EVIDENCE,
)
from commander_agent.skills.registry import SKILLS, build_system_prompt, load_skill_instructions
from commander_agent.reasoning.local_ollama import role_chat
from commander_agent.skills.strategy import compose_query_strategy, strategy_prompt_text
from commander_agent.mcp.adapter import get_tool_name, get_tool_arguments
from commander_agent.mcp.results import clip_text
from commander_agent.enrichment.web import web_search_public, fetch_public_url
from commander_agent.state.io import stable_hash
from commander_agent.state.cache import save_validated_answer
from commander_agent.state.plan import (
    update_plan_phase,
    phase_entry,
    add_dynamic_todo,
    complete_next_todo,
    complete_todo_by_text,
)
from commander_agent.state.evidence import (
    new_case_state,
    record_evidence,
    evidence_model_text,
    append_validation_record,
    current_evidence_version,
    current_progress_version,
    candidate_is_quarantined,
)
from commander_agent.state.timeline import (
    query_timeline,
    set_anchor,
    current_anchor,
    timeline_before,
    timeline_after,
    timeline_between,
    timeline_entities,
    timeline_related,
    timeline_map,
    timeline_context_for_question,
)
from commander_agent.state.validation import validation_review, validation_feedback_message
from commander_agent.evaluation.metrics import print_observation_summary
from commander_agent.core.context import build_turn_context
from commander_agent.core.executor import execute_splunk_search, execute_bootstrap_strategy
from commander_agent.core.semantic_recovery import run_semantic_recovery, semantic_review_feedback
from commander_agent.core.temporal_analysis import run_temporal_analysis
from commander_agent.state.scope import build_scope_contract, render_scope_contract
from commander_agent.state.semantic_state import record_investigation_frame, record_semantic_bindings
from commander_agent.state.extraction import (
    record_extraction_contract, derive_extraction_candidate, build_dynamic_check_query, verify_extraction_candidate,
)
from commander_agent.skills.investigation_supervisor.scripts.reviewer import review_investigation_progress


def _new_metrics(seed=None):
    metrics = dict(seed or {})
    defaults = {
        "rounds_used": 0,
        "tool_calls": 0,
        "search_attempts": 0,
        "search_calls": 0,
        "query_cache_hits": 0,
        "current_run_cache_hits": 0,
        "guardrail_blocks": 0,
        "skill_contract_blocks": 0,
        "retry_diversity_blocks": 0,
        "structured_output_rejections": 0,
        "zero_results": 0,
        "tool_errors": 0,
        "fatal_tool_errors": 0,
        "external_calls": 0,
        "timeline_cache_calls": 0,
        "timeline_cache_hits": 0,
        "timeline_anchor_sets": 0,
        "timeline_before_calls": 0,
        "timeline_after_calls": 0,
        "timeline_relationship_calls": 0,
        "map_first_reuse_checks": 0,
        "map_first_reuse_hits": 0,
        "evidence_items": 0,
        "observations_stored": 0,
        "timeline_rows": 0,
        "candidate_answers": 0,
        "validation_reviews": 0,
        "measurement_resolution_calls": 0,
        "measurement_resolution_failures": 0,
        "measurement_resolution_unresolved": 0,
        "measurement_contracts_created": 0,
        "deterministic_candidate_derivations": 0,
        "measurement_drift_blocks": 0,
        "investigation_frame_calls": 0,
        "investigation_frame_failures": 0,
        "semantic_bindings_created": 0,
        "semantic_binding_contract_promotions": 0,
        "semantic_binding_invalidations": 0,
        "semantic_conflicts_detected": 0,
        "semantic_binding_query_blocks": 0,
        "semantic_query_policy_blocks": 0,
        "multivalue_cardinality_blocks": 0,
        "ranking_population_blocks": 0,
        "source_contract_blocks": 0,
        "extraction_contract_blocks": 0,
        "discovery_specificity_blocks": 0,
        "extraction_contracts_created": 0,
        "direct_extraction_derivations": 0,
        "direct_extraction_verifications": 0,
        "supervisor_query_reviews": 0,
        "supervisor_relationship_review_failures": 0,
        "bootstrap_queries": 0,
        "deterministic_resolutions": 0,
        "deterministic_validation_blocks": 0,
        "semantic_skill_reviews": 0,
        "semantic_skill_failures": 0,
        "semantic_skill_query_actions": 0,
        "semantic_skill_accepts": 0,
        "semantic_validation_blocks": 0,
        "semantic_format_repairs": 0,
        "semantic_format_repair_successes": 0,
        "semantic_action_contract_rejections": 0,
        "semantic_action_repairs": 0,
        "semantic_action_repair_successes": 0,
        "scope_guard_reviews": 0,
        "scope_guard_allows": 0,
        "scope_guard_denials": 0,
        "scope_guard_failures": 0,
        "scope_guard_blocks": 0,
        "supervisor_reviews": 0,
        "supervisor_failures": 0,
        "supervisor_fallbacks": 0,
        "supervisor_redirects": 0,
        "supervisor_stop_allows": 0,
        "supervisor_stop_denials": 0,
        "supervisor_validation_allows": 0,
        "canonical_release_allows": 0,
        "supervisor_eligible_snapshots": 0,
        "capability_assignments": 0,
        "capability_deferrals": 0,
        "capability_failovers": 0,
        "investigator_contract_failures": 0,
        "recovery_budget_exhausted": 0,
        "temporal_analysis_calls": 0,
        "temporal_analysis_rows": 0,
        "temporal_analysis_groups": 0,
        "temporal_graphs": 0,
        "semantic_stagnation_blocks": 0,
        "candidate_quarantines": 0,
        "agent_no_evidence_blocks": 0,
        "context_compactions": 0,
        "max_estimated_prompt_tokens": 0,
        "ollama_retries": 0,
        "ollama_truncation_detected": 0,
        "ollama_truncation_retries": 0,
        "ollama_investigator_prompt_tokens": 0,
        "ollama_investigator_output_tokens": 0,
        "ollama_supervisor_prompt_tokens": 0,
        "ollama_supervisor_output_tokens": 0,
        "remote_candidate_fastpath": 0,
        "openai_reasoning_calls": 0,
        "openai_reasoning_failures": 0,
        "queries": [],
        "final_validation_status": "not reached",
    }
    for key, value in defaults.items():
        metrics.setdefault(key, value)
    return metrics


def _current_phase(plan):
    do_phase = phase_entry(plan, "DO")
    check_phase = phase_entry(plan, "CHECK")
    if do_phase and do_phase.get("status") != "completed":
        return "DO"
    if check_phase and check_phase.get("status") != "completed":
        return "CHECK"
    return "REVIEW"


async def _ollama_turn(messages, tools, metrics):
    try:
        return role_chat(
            role="investigator",
            messages=messages,
            tools=tools,
            metrics=metrics,
            options={"num_ctx": OLLAMA_NUM_CTX, "temperature": 0},
        )
    except Exception as first_exc:
        metrics["ollama_retries"] += 1
        print(f"[Ollama Investigator turn retry after {type(first_exc).__name__}: {first_exc}]")
        await asyncio.sleep(1.0)
        try:
            return role_chat(
                role="investigator",
                messages=messages,
                tools=tools,
                metrics=metrics,
                options={"num_ctx": min(OLLAMA_NUM_CTX, 6144), "temperature": 0},
                think=False,
            )
        except Exception:
            raise first_exc


def _timeline_result_evidence(case_state, name, arguments, payload, method_class):
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    return record_evidence(
        case_state,
        "timeline",
        name,
        text,
        query=json.dumps(arguments, ensure_ascii=False),
        signature=stable_hash({"tool": name, "arguments": arguments, "case": case_state.get("case_id")}),
        cache_label="reusable timeline work",
        method_class=method_class,
    ), text


def _stop_for_fatal_tool_error(case_state, metrics, active_skills, plan):
    fatal = case_state.get("fatal_tool_error")
    if not fatal:
        return None
    kind = fatal.get("kind", "tool_error")
    message = fatal.get("message", "A non-retryable Splunk failure occurred.")
    metrics["final_validation_status"] = f"fatal_{kind}"
    update_plan_phase(plan, "CONTINUE/STOP", "completed", f"STOP: {kind}: {message}")
    print("\n" + "=" * 70)
    print("INVESTIGATION STOPPED - NON-RETRYABLE SPLUNK FAILURE")
    print("=" * 70)
    print(f"Type   : {kind}")
    print(f"Code   : {fatal.get('code', 'SPLUNK_FATAL_ERROR')}")
    print(f"Reason : {message}")
    print("Action : Fix the local Splunk credentials/permissions or dataset, then rerun.")
    metrics["evidence_items"] = len(case_state.get("evidence", []))
    metrics["timeline_rows"] = case_state.get("timeline_rows", 0)
    print_observation_summary(metrics, active_skills)
    return (None, metrics, active_skills, plan)


def _finalize_validated_candidate(question, candidate, review, case_state, metrics, active_skills, plan):
    support_ids = review.get("support_ids", [])
    metrics["final_validation_status"] = "validated"
    metrics["validated_support_ids"] = list(support_ids)
    metrics["validated_sequence_evidence"] = [
        {k: e.get(k) for k in ("evidence_id", "query", "summary", "result_excerpt") if k in e}
        for e in case_state.get("evidence", []) if e.get("evidence_id") in support_ids
    ]
    append_validation_record(case_state, candidate, review, "validated")
    update_plan_phase(plan, "DO", "completed", "Primary evidence obtained.", support_ids[:1])
    update_plan_phase(plan, "CHECK", "completed", "Independent evidence is consistent.", support_ids)
    update_plan_phase(plan, "REVIEW", "completed", review.get("summary", "Review passed."), support_ids)
    update_plan_phase(plan, "CONTINUE/STOP", "completed", "STOP: promoted from work to validated work.", support_ids)
    complete_todo_by_text(plan, "Audit evidence", support_ids, "Review completed and candidate validated.")
    save_validated_answer(question, candidate, plan, review, metrics, active_skills)

    print("\n" + "=" * 70)
    print("PROMOTED TO VALIDATED WORK")
    print("=" * 70)
    print("Independent support: " + ", ".join(support_ids))
    print("\n" + "=" * 70)
    print("FINAL ANSWER")
    print("=" * 70)
    print(candidate)
    metrics["evidence_items"] = len(case_state.get("evidence", []))
    metrics["timeline_rows"] = case_state.get("timeline_rows", 0)
    metrics["evidence_version"] = current_evidence_version(case_state)
    print_observation_summary(metrics, active_skills)
    return candidate, metrics, active_skills, plan


async def _attempt_direct_extraction_release(
    *, session, question, query_strategy, case_state, metrics, active_skills, plan, reasoner
):
    """Run the ALR legacy compatibility deterministic direct-extraction state machine.

    This is invoked after bootstrap and after every later machine-evidence search.
    The model may choose evidence routes, but it never owns the extracted value.
    """
    if (query_strategy or {}).get("resolver") != "direct_extraction":
        return None

    derivations_before = len(case_state.get("extraction_derivations", []))
    derivation = derive_extraction_candidate(case_state)
    if not derivation:
        return None
    if len(case_state.get("extraction_derivations", [])) > derivations_before:
        metrics["direct_extraction_derivations"] = metrics.get("direct_extraction_derivations", 0) + 1
        metrics["analytical_progress"] = metrics.get("analytical_progress", 0) + 1

    # First let any already-executed static CHECK evidence verify the derivation.
    verifications_before = len(case_state.get("extraction_verifications", []))
    verification = verify_extraction_candidate(case_state, derivation)

    # If static evidence is insufficient, issue one deterministic exact check derived
    # solely from the active contract + observed machine row.
    if not verification and (query_strategy or {}).get("dynamic_check"):
        dynamic_check = build_dynamic_check_query(case_state, derivation)
        if dynamic_check:
            print("\n" + "-" * 70)
            print("ALR legacy compatibility DIRECT EXTRACTION: DYNAMIC CHECK")
            print("-" * 70)
            print(dynamic_check)
            metrics["bootstrap_queries"] += 1
            _, evidence, _ = await execute_splunk_search(
                session=session, tool_name="search_oneshot",
                arguments={"query": dynamic_check, "earliest_time": "0", "latest_time": "now", "max_count": 50, "output_format": "json"},
                case_state=case_state, metrics=metrics, round_number=0, active_skills=active_skills,
                phase="CHECK", question=question,
            )
            if evidence:
                check_evidence_id = evidence.get("evidence_id")
                update_plan_phase(plan, "CHECK", "completed", "Deterministic extraction check executed.", [check_evidence_id])
                complete_next_todo(plan, [check_evidence_id], "Deterministic direct-extraction check completed.")
                verification = verify_extraction_candidate(case_state, derivation, check_evidence_id=check_evidence_id)

    if not verification:
        return None
    if len(case_state.get("extraction_verifications", [])) > verifications_before:
        metrics["direct_extraction_verifications"] = metrics.get("direct_extraction_verifications", 0) + 1
        metrics["analytical_progress"] = metrics.get("analytical_progress", 0) + 1

    candidate = str(derivation.get("candidate") or "").strip()
    counted = case_state.setdefault("deterministic_direct_candidates_counted", set())
    fp = str(derivation.get("fingerprint") or candidate)
    if fp not in counted:
        counted.add(fp)
        metrics["candidate_answers"] += 1

    supervisor = review_investigation_progress(
        question=question, case_state=case_state, metrics=metrics,
        last_action={"action": "deterministic_direct_extraction"},
        last_result={"status": "verified", "support_ids": verification.get("support_ids")},
        candidate=candidate, reasoner=reasoner,
    )
    print("\n" + "=" * 70)
    print("SUPERVISOR EVALUATION")
    print("=" * 70)
    print(json.dumps(supervisor, indent=2, ensure_ascii=False))
    if not supervisor.get("allow_validation"):
        return None

    review = validation_review(question, candidate, plan, case_state, metrics, reasoner=reasoner)
    print("\nVALIDATION REVIEW:")
    print(json.dumps(review, indent=2, ensure_ascii=False))
    if review.get("decision") == "accept" and len(review.get("support_ids", [])) >= MIN_VALIDATION_SUPPORT:
        return _finalize_validated_candidate(question, candidate, review, case_state, metrics, active_skills, plan)
    return None


def _stop_semantic_unresolved(case_state, metrics, active_skills, plan, semantic_review=None, note=""):
    metrics["final_validation_status"] = "semantic_unresolved"
    metrics["evidence_items"] = len(case_state.get("evidence", []))
    metrics["timeline_rows"] = case_state.get("timeline_rows", 0)
    metrics["evidence_version"] = current_evidence_version(case_state)
    reason = note or str((semantic_review or {}).get("reason") or "Semantic evidence remains unresolved.")
    support_ids = list((semantic_review or {}).get("support_ids") or [])
    update_plan_phase(
        plan,
        "REVIEW",
        "completed",
        "Semantic review completed without a validated candidate.",
        support_ids,
    )
    complete_todo_by_text(
        plan,
        "Audit evidence",
        support_ids,
        "Semantic audit completed; evidence remains unresolved.",
    )
    update_plan_phase(
        plan,
        "CONTINUE/STOP",
        "completed",
        "STOP: no supported candidate after bounded semantic recovery.",
        support_ids,
    )
    print("\n" + "=" * 70)
    print("STOPPED - SEMANTIC EVIDENCE UNRESOLVED")
    print("=" * 70)
    print("Reason : " + reason)
    if semantic_review:
        print("Decision: " + str(semantic_review.get("decision", "")))
        print("Status  : " + str(semantic_review.get("orchestration_status", "")))
    print("Action : Obtain genuinely new evidence or improve the semantic skill; unsupported candidates are not returned.")
    print_observation_summary(metrics, active_skills)
    return None, metrics, active_skills, plan


async def investigate(
    session,
    ollama_tools,
    question,
    initial_skills,
    plan,
    prior_context,
    query_strategy=None,
    seed_metrics=None,
    reasoner=None,
):
    active_skills = list(initial_skills)
    case_state = new_case_state(plan, active_skills=active_skills)
    metrics = _new_metrics(seed_metrics)
    if case_state.get("investigation_frame"):
        record_investigation_frame(case_state, case_state.get("investigation_frame"))
    if case_state.get("semantic_bindings"):
        # Persist plan-time bindings in the case ledger before any machine evidence arrives.
        existing = list(case_state.get("semantic_bindings") or [])
        case_state["semantic_bindings"] = []
        record_semantic_bindings(case_state, existing)

    if case_state.get("extraction_contracts"):
        existing_contract = dict(case_state.get("extraction_contracts")[-1])
        case_state["extraction_contracts"] = []
        record_extraction_contract(case_state, existing_contract)
        metrics["extraction_contracts_created"] = metrics.get("extraction_contracts_created", 0) + 1

    query_strategy = query_strategy or compose_query_strategy(
        question,
        active_skills,
        investigation_frame=case_state.get("investigation_frame"),
        semantic_bindings=case_state.get("semantic_bindings"),
    )
    case_state["scope_contract"] = build_scope_contract(
        query_strategy.get("primary_query", ""),
        query_strategy.get("check_query", ""),
    )
    strategy_guidance = strategy_prompt_text(query_strategy)
    if case_state["scope_contract"].get("base_scope_spl"):
        strategy_guidance += "\nImmutable query scope contract:\n" + render_scope_contract(case_state["scope_contract"])
    system_prompt = build_system_prompt(active_skills)
    feedback = ""
    last_candidate = None
    evidence_count_at_last_candidate = -1

    # Map-first reuse is executable: check the persistent case map before spending a
    # Splunk call. It remains WORK until its backing evidence/review supports a claim.
    timeline_context = timeline_context_for_question(question)
    if "bidirectional_timeframe" in active_skills:
        metrics["map_first_reuse_checks"] += 1
        if timeline_context:
            metrics["map_first_reuse_hits"] += 1
            metrics["timeline_cache_hits"] += 1
            complete_todo_by_text(plan, "Reuse existing case-map", note="Relevant persistent map rows found.")
            feedback = (
                "MAP-FIRST REUSE: relevant timeline/case-map rows already exist. Inspect them with "
                "query_timeline/timeline_map/timeline_related before running new Splunk searches."
            )

    if (
        ENABLE_BOOTSTRAP_QUERIES
        and query_strategy.get("bootstrap")
        and query_strategy.get("confidence") == "high"
    ):
        support_ids, outputs, finding = await execute_bootstrap_strategy(
            session,
            query_strategy,
            case_state,
            metrics,
            active_skills,
            question,
        )
        fatal_stop = _stop_for_fatal_tool_error(case_state, metrics, active_skills, plan)
        if fatal_stop is not None:
            return fatal_stop
        if support_ids:
            primary_ids = support_ids[:1]
            check_ids = support_ids[1:2]
            update_plan_phase(plan, "DO", "completed", "Primary skill query executed.", primary_ids)
            complete_next_todo(plan, primary_ids, "Primary skill query completed.")
            if check_ids:
                update_plan_phase(plan, "CHECK", "completed", "Independent skill check executed.", check_ids)
                complete_next_todo(plan, check_ids, "Independent cross-check completed.")
            else:
                update_plan_phase(plan, "CHECK", "in_progress", "Primary evidence exists; independent check still needed.")
            feedback = (
                "The harness already executed the high-confidence primary/check skill strategy. "
                "Inspect the compact evidence and structured finding before proposing a candidate."
            )
            # ALR legacy compatibility direct-extraction path: Python derives and verifies the value
            # before a free-form answer model can anchor on bootstrap evidence.
            direct_result = await _attempt_direct_extraction_release(
                session=session, question=question, query_strategy=query_strategy,
                case_state=case_state, metrics=metrics, active_skills=active_skills,
                plan=plan, reasoner=reasoner,
            )
            if direct_result is not None:
                return direct_result

            # ALR legacy compatibility makes the semantic skill authoritative and executable before
            # any free-form candidate model is allowed to anchor on the bootstrap result.
            if query_strategy.get("semantic_review_skill") == "result_semantics_recovery":
                if "result_semantics_recovery" not in active_skills:
                    active_skills.append("result_semantics_recovery")
                    case_state["active_skills"] = list(active_skills)
                    system_prompt = build_system_prompt(active_skills)

                semantic_review = await run_semantic_recovery(
                    session=session,
                    question=question,
                    query_strategy=query_strategy,
                    case_state=case_state,
                    metrics=metrics,
                    active_skills=active_skills,
                    reasoner=reasoner,
                    candidate="",
                )
                fatal_stop = _stop_for_fatal_tool_error(case_state, metrics, active_skills, plan)
                if fatal_stop is not None:
                    return fatal_stop
                if not semantic_review:
                    return _stop_semantic_unresolved(
                        case_state, metrics, active_skills, plan,
                        note="The result-semantics skill did not produce a valid decision after structured-output repair.",
                    )
                feedback = semantic_review_feedback(semantic_review)
                if semantic_review.get("decision") in {"STOP_ERROR", "STOP_UNRESOLVED"}:
                    return _stop_semantic_unresolved(case_state, metrics, active_skills, plan, semantic_review)

                if (
                    semantic_review.get("decision") in {"ACCEPT", "REINTERPRET"}
                    and semantic_review.get("candidate_status") == "supported"
                    and str(semantic_review.get("candidate") or "").strip()
                ):
                    candidate = str(semantic_review.get("candidate")).strip()
                    metrics["candidate_answers"] += 1
                    review = validation_review(
                        question,
                        candidate,
                        plan,
                        case_state,
                        metrics,
                        reasoner=reasoner,
                        semantic_gate=semantic_review,
                    )
                    print("\nVALIDATION REVIEW:")
                    print(json.dumps(review, indent=2, ensure_ascii=False))
                    if review.get("decision") == "accept" and len(review.get("support_ids", [])) >= MIN_VALIDATION_SUPPORT:
                        return _finalize_validated_candidate(
                            question, candidate, review, case_state, metrics, active_skills, plan
                        )

                    # ALR legacy compatibility: validation feedback is actionable. If the semantic candidate is
                    # plausible but lacks materially independent support, return that exact gap
                    # to the semantic state machine so it can choose a scoped query or deterministic
                    # temporal analysis. Do not stop merely because the first candidate arrived
                    # before its cross-check.
                    add_dynamic_todo(
                        plan,
                        review.get("next_action") or "Obtain materially independent evidence for the semantic candidate.",
                    )
                    followup_semantic = await run_semantic_recovery(
                        session=session,
                        question=question,
                        query_strategy=query_strategy,
                        case_state=case_state,
                        metrics=metrics,
                        active_skills=active_skills,
                        reasoner=reasoner,
                        candidate=candidate,
                        validation_feedback=review,
                    )
                    fatal_stop = _stop_for_fatal_tool_error(case_state, metrics, active_skills, plan)
                    if fatal_stop is not None:
                        return fatal_stop
                    if (
                        followup_semantic
                        and followup_semantic.get("decision") in {"ACCEPT", "REINTERPRET"}
                        and followup_semantic.get("candidate_status") == "supported"
                        and str(followup_semantic.get("candidate") or "").strip()
                    ):
                        candidate = str(followup_semantic.get("candidate")).strip()
                        metrics["candidate_answers"] += 1
                        review2 = validation_review(
                            question,
                            candidate,
                            plan,
                            case_state,
                            metrics,
                            reasoner=reasoner,
                            semantic_gate=followup_semantic,
                        )
                        print("\nVALIDATION REVIEW AFTER RECOVERY:")
                        print(json.dumps(review2, indent=2, ensure_ascii=False))
                        if review2.get("decision") == "accept" and len(review2.get("support_ids", [])) >= MIN_VALIDATION_SUPPORT:
                            return _finalize_validated_candidate(
                                question, candidate, review2, case_state, metrics, active_skills, plan
                            )
                        return _stop_semantic_unresolved(
                            case_state, metrics, active_skills, plan, followup_semantic,
                            note=review2.get("summary") or "Independent evidence remained insufficient after recovery.",
                        )
                    return _stop_semantic_unresolved(
                        case_state, metrics, active_skills, plan, followup_semantic or semantic_review,
                        note=(review.get("summary") or "Semantic candidate did not satisfy the final evidence gate."),
                    )

                # The semantic state machine has already executed any explicit query
                # actions it chose. Do not hand the same unresolved evidence to the
                # free-form answer model and start an argument loop.
                return _stop_semantic_unresolved(case_state, metrics, active_skills, plan, semantic_review)

    last_agent_evidence_version = None
    turns_without_new_evidence = 0

    for round_number in range(1, MAX_AGENT_ROUNDS + 1):
        metrics["rounds_used"] = round_number
        current_version = current_progress_version(case_state)
        if last_agent_evidence_version == current_version:
            turns_without_new_evidence += 1
        else:
            turns_without_new_evidence = 0
        last_agent_evidence_version = current_version
        if turns_without_new_evidence > MAX_AGENT_TURNS_WITHOUT_NEW_EVIDENCE:
            metrics["agent_no_evidence_blocks"] = metrics.get("agent_no_evidence_blocks", 0) + 1
            metrics["final_validation_status"] = "stagnation_no_new_evidence"
            update_plan_phase(plan, "CONTINUE/STOP", "completed", "STOP: agent loop produced no new evidence.")
            print("\n" + "=" * 70)
            print("STOPPED - NO NEW EVIDENCE OR ANALYTICAL PROGRESS")
            print("=" * 70)
            print("The free-form agent was not allowed to continue reasoning over unchanged evidence/state.")
            metrics["evidence_items"] = len(case_state.get("evidence", []))
            metrics["timeline_rows"] = case_state.get("timeline_rows", 0)
            print_observation_summary(metrics, active_skills)
            return None, metrics, active_skills, plan
        print(f"\n================ ALR legacy compatibility AGENT ROUND {round_number} ================")

        timeline_context = timeline_context_for_question(question)
        turn_content, budget = build_turn_context(
            question=question,
            plan=plan,
            case_state=case_state,
            prior_context=prior_context,
            timeline_context=timeline_context,
            strategy_guidance=strategy_guidance,
            feedback=feedback,
            system_prompt=system_prompt,
        )
        metrics["max_estimated_prompt_tokens"] = max(
            metrics["max_estimated_prompt_tokens"], budget["estimated_prompt_tokens"]
        )
        if budget["context_compacted"]:
            metrics["context_compactions"] += 1
        print(
            f"[Context estimate: ~{budget['estimated_prompt_tokens']} tokens; "
            f"compacted={budget['context_compacted']}]"
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": turn_content},
        ]

        try:
            response = await _ollama_turn(messages, ollama_tools, metrics)
        except Exception as exc:
            print("\nOLLAMA ERROR:")
            print(f"{type(exc).__name__}: {exc}")
            metrics["timeline_rows"] = case_state.get("timeline_rows", 0)
            metrics["evidence_items"] = len(case_state["evidence"])
            print_observation_summary(metrics, active_skills)
            return None, metrics, active_skills, plan

        prompt_tokens = getattr(response, "prompt_eval_count", None)
        eval_tokens = getattr(response, "eval_count", None)
        if prompt_tokens is not None:
            metrics["ollama_prompt_tokens"] = metrics.get("ollama_prompt_tokens", 0) + int(prompt_tokens or 0)
            metrics["ollama_output_tokens"] = metrics.get("ollama_output_tokens", 0) + int(eval_tokens or 0)
            print(f"[Ollama prompt tokens: {prompt_tokens}; generated: {eval_tokens}]")

        assistant_message = response.message
        if assistant_message.content:
            print("\nMODEL:")
            print(assistant_message.content)

        tool_calls = assistant_message.tool_calls or []

        if not tool_calls:
            candidate = assistant_message.content or ""
            metrics["candidate_answers"] += 1
            repeated_without_evidence = len(case_state["evidence"]) == evidence_count_at_last_candidate
            evidence_count_at_last_candidate = len(case_state["evidence"])
            last_candidate = candidate

            update_plan_phase(plan, "REVIEW", "in_progress", "Independent validation review of candidate answer.")
            print("\n" + "=" * 70)
            print("CHECK / REVIEW: CANDIDATE IS STILL WORK")
            print("=" * 70)
            print(candidate if candidate else "[No candidate text.]")

            semantic_gate = None
            if "result_semantics_recovery" in active_skills:
                if candidate_is_quarantined(case_state, candidate):
                    metrics["agent_no_evidence_blocks"] = metrics.get("agent_no_evidence_blocks", 0) + 1
                    return _stop_semantic_unresolved(
                        case_state,
                        metrics,
                        active_skills,
                        plan,
                        note="The same candidate was already rejected at the current evidence version.",
                    )

                semantic_gate = await run_semantic_recovery(
                    session=session,
                    question=question,
                    query_strategy=query_strategy,
                    case_state=case_state,
                    metrics=metrics,
                    active_skills=active_skills,
                    reasoner=reasoner,
                    candidate=candidate,
                )
                fatal_stop = _stop_for_fatal_tool_error(case_state, metrics, active_skills, plan)
                if fatal_stop is not None:
                    return fatal_stop

                if not semantic_gate:
                    # One evidence-level attempt may still choose a useful query. The
                    # per-evidence-version budget prevents an open-ended retry loop.
                    semantic_gate = await run_semantic_recovery(
                        session=session,
                        question=question,
                        query_strategy=query_strategy,
                        case_state=case_state,
                        metrics=metrics,
                        active_skills=active_skills,
                        reasoner=reasoner,
                        candidate="",
                    )

                if not semantic_gate:
                    return _stop_semantic_unresolved(
                        case_state,
                        metrics,
                        active_skills,
                        plan,
                        note="The semantic skill remained unavailable after its bounded repair/review budget.",
                    )

                if (
                    semantic_gate.get("decision") not in {"ACCEPT", "REINTERPRET"}
                    or semantic_gate.get("candidate_status") != "supported"
                    or not str(semantic_gate.get("candidate") or "").strip()
                ):
                    # If a candidate was rejected, give the skill (not the answer model)
                    # one evidence-level chance to choose a recovery action.
                    if semantic_gate.get("decision") == "REJECT_CANDIDATE":
                        recovery = await run_semantic_recovery(
                            session=session,
                            question=question,
                            query_strategy=query_strategy,
                            case_state=case_state,
                            metrics=metrics,
                            active_skills=active_skills,
                            reasoner=reasoner,
                            candidate="",
                        )
                        if recovery:
                            semantic_gate = recovery
                    if (
                        not semantic_gate
                        or semantic_gate.get("decision") not in {"ACCEPT", "REINTERPRET"}
                        or semantic_gate.get("candidate_status") != "supported"
                        or not str(semantic_gate.get("candidate") or "").strip()
                    ):
                        return _stop_semantic_unresolved(
                            case_state, metrics, active_skills, plan, semantic_gate
                        )

                # Canonical answer comes from the skill that interpreted the evidence,
                # not from potentially anchored free-form prose.
                candidate = str(semantic_gate.get("candidate")).strip()
                last_candidate = candidate

            review = validation_review(
                question,
                candidate,
                plan,
                case_state,
                metrics,
                reasoner=reasoner,
                semantic_gate=semantic_gate,
            )
            print("\nVALIDATION REVIEW:")
            print(json.dumps(review, indent=2, ensure_ascii=False))

            support_ids = review.get("support_ids", [])
            enough_support = len(support_ids) >= MIN_VALIDATION_SUPPORT
            budget_exhausted = metrics["search_calls"] >= MAX_SEARCH_EXECUTIONS or round_number >= MAX_AGENT_ROUNDS

            if review.get("decision") == "accept" and enough_support:
                return _finalize_validated_candidate(
                    question, candidate, review, case_state, metrics, active_skills, plan
                )

            if "result_semantics_recovery" in active_skills:
                # Semantic workflows fail closed instead of spending more agent turns
                # restating a candidate against unchanged evidence.
                return _stop_semantic_unresolved(
                    case_state,
                    metrics,
                    active_skills,
                    plan,
                    semantic_gate,
                    note=review.get("summary") or "Final semantic validation did not pass.",
                )

            if budget_exhausted:
                metrics["final_validation_status"] = "limited_validation"
                append_validation_record(case_state, candidate, review, "limited_validation")
                update_plan_phase(plan, "REVIEW", "completed", "Bounded search/round limit reached.", support_ids)
                update_plan_phase(plan, "CONTINUE/STOP", "completed", "STOP: limited validation; not cached as validated work.", support_ids)
                print("\n" + "=" * 70)
                print("FINAL ANSWER - LIMITED VALIDATION")
                print("=" * 70)
                print(candidate)
                metrics["evidence_items"] = len(case_state["evidence"])
                metrics["timeline_rows"] = case_state.get("timeline_rows", 0)
                print_observation_summary(metrics, active_skills)
                return candidate, metrics, active_skills, plan

            append_validation_record(case_state, candidate, review, "needs_more_work")
            next_action = review.get("next_action") or "Run one materially different targeted check."
            add_dynamic_todo(plan, next_action, review.get("summary", "Validation gap."))
            update_plan_phase(plan, "CHECK", "in_progress", next_action, support_ids)
            update_plan_phase(plan, "REVIEW", "pending", "Review again after new evidence.")
            update_plan_phase(plan, "CONTINUE/STOP", "in_progress", "CONTINUE: a validation gap remains.")
            feedback = validation_feedback_message(review)
            if repeated_without_evidence:
                feedback += "\nThe candidate was repeated without new evidence. Use a tool before proposing it again."
            continue

        feedback_parts = []
        for tool_call in tool_calls:
            metrics["tool_calls"] += 1
            name = get_tool_name(tool_call)
            arguments = get_tool_arguments(tool_call)
            print("\n" + "-" * 70)
            print(f"TOOL: {name}")
            print("-" * 70)
            print(json.dumps(arguments, indent=2, ensure_ascii=False))

            if name == "run_temporal_analysis":
                if "temporal_pattern_analysis" not in active_skills:
                    active_skills.append("temporal_pattern_analysis")
                    case_state["active_skills"] = list(active_skills)
                    system_prompt = build_system_prompt(active_skills)
                analysis, evidence = await run_temporal_analysis(
                    session=session,
                    question=question,
                    case_state=case_state,
                    metrics=metrics,
                    active_skills=active_skills,
                    request=arguments,
                )
                if evidence:
                    feedback_parts.append(
                        evidence_model_text(
                            evidence["evidence_id"],
                            evidence["cache_label"],
                            clip_text(json.dumps(analysis, ensure_ascii=False, indent=2), MAX_TOTAL_MODEL_RESULT_CHARS),
                            "Deterministic temporal features; reason from the structured values, not graph pixels.",
                        )
                    )
                else:
                    feedback_parts.append(clip_text(json.dumps(analysis, ensure_ascii=False, indent=2), 3000))
                continue

            if name == "load_skill":
                skill_name = arguments.get("skill_name")
                if skill_name in SKILLS:
                    if skill_name not in active_skills:
                        active_skills.append(skill_name)
                        case_state["active_skills"] = list(active_skills)
                    system_prompt = build_system_prompt(active_skills)
                    query_strategy = compose_query_strategy(question, active_skills)
                    case_state["scope_contract"] = build_scope_contract(
                        query_strategy.get("primary_query", ""),
                        query_strategy.get("check_query", ""),
                    )
                    strategy_guidance = strategy_prompt_text(query_strategy)
                    if case_state["scope_contract"].get("base_scope_spl"):
                        strategy_guidance += "\nImmutable query scope contract:\n" + render_scope_contract(case_state["scope_contract"])
                    result_text = f"Skill loaded: {skill_name}\n\n" + load_skill_instructions(skill_name)
                else:
                    result_text = "Unknown skill. Choose one of: " + ", ".join(sorted(SKILLS.keys()))
                feedback_parts.append(clip_text(result_text, 1800))
                continue

            if name == "set_timeline_anchor":
                result = set_anchor(
                    case_state,
                    arguments.get("event_time"),
                    evidence_id=arguments.get("evidence_id") or None,
                    reason=arguments.get("reason") or "Evidence-backed anchor selected by investigation",
                    entity=arguments.get("entity") or None,
                )
                if result.get("ok"):
                    metrics["timeline_anchor_sets"] += 1
                    complete_todo_by_text(plan, "Establish an evidence-backed T0", note="T0 set and versioned.")
                feedback_parts.append(clip_text(json.dumps(result, ensure_ascii=False, indent=2), 2200))
                continue

            if name == "query_timeline":
                metrics["timeline_cache_calls"] += 1
                rows = query_timeline(
                    terms=arguments.get("terms") or [],
                    case_id=arguments.get("case_id") or None,
                    entity=arguments.get("entity") or None,
                    start_time=arguments.get("start_time") or None,
                    end_time=arguments.get("end_time") or None,
                    limit=arguments.get("limit") or 20,
                )
                if rows:
                    metrics["timeline_cache_hits"] += 1
                evidence, text = _timeline_result_evidence(case_state, name, arguments, {"events": rows}, "timeline_lookup")
                feedback_parts.append(evidence_model_text(evidence["evidence_id"], evidence["cache_label"], clip_text(text, MAX_TOTAL_MODEL_RESULT_CHARS)))
                continue

            if name == "timeline_before":
                metrics["timeline_cache_calls"] += 1
                metrics["timeline_before_calls"] += 1
                case_id = arguments.get("case_id") or case_state.get("case_id")
                payload = timeline_before(case_id, minutes=arguments.get("minutes") or 30, entity=arguments.get("entity") or None, limit=arguments.get("limit") or 50)
                if payload.get("events"):
                    metrics["timeline_cache_hits"] += 1
                evidence, text = _timeline_result_evidence(case_state, name, arguments, payload, "timeline_before")
                feedback_parts.append(evidence_model_text(evidence["evidence_id"], evidence["cache_label"], clip_text(text, MAX_TOTAL_MODEL_RESULT_CHARS)))
                continue

            if name == "timeline_after":
                metrics["timeline_cache_calls"] += 1
                metrics["timeline_after_calls"] += 1
                case_id = arguments.get("case_id") or case_state.get("case_id")
                payload = timeline_after(case_id, minutes=arguments.get("minutes") or 30, entity=arguments.get("entity") or None, limit=arguments.get("limit") or 50)
                if payload.get("events"):
                    metrics["timeline_cache_hits"] += 1
                evidence, text = _timeline_result_evidence(case_state, name, arguments, payload, "timeline_after")
                feedback_parts.append(evidence_model_text(evidence["evidence_id"], evidence["cache_label"], clip_text(text, MAX_TOTAL_MODEL_RESULT_CHARS)))
                continue

            if name == "timeline_between":
                metrics["timeline_cache_calls"] += 1
                rows = timeline_between(
                    arguments.get("start_time"), arguments.get("end_time"),
                    case_id=arguments.get("case_id") or None,
                    entity=arguments.get("entity") or None,
                    limit=arguments.get("limit") or 100,
                )
                if rows:
                    metrics["timeline_cache_hits"] += 1
                evidence, text = _timeline_result_evidence(case_state, name, arguments, {"events": rows}, "timeline_between")
                feedback_parts.append(evidence_model_text(evidence["evidence_id"], evidence["cache_label"], clip_text(text, MAX_TOTAL_MODEL_RESULT_CHARS)))
                continue

            if name == "timeline_entities":
                metrics["timeline_cache_calls"] += 1
                rows = timeline_entities(case_id=arguments.get("case_id") or None, limit=arguments.get("limit") or 30)
                if rows:
                    metrics["timeline_cache_hits"] += 1
                feedback_parts.append(clip_text(json.dumps(rows, ensure_ascii=False, indent=2), 3000))
                continue

            if name == "timeline_related":
                metrics["timeline_cache_calls"] += 1
                metrics["timeline_relationship_calls"] += 1
                rows = timeline_related(arguments.get("entity", ""), case_id=arguments.get("case_id") or None, limit=arguments.get("limit") or 50)
                if rows:
                    metrics["timeline_cache_hits"] += 1
                evidence, text = _timeline_result_evidence(case_state, name, arguments, {"relationships": rows}, "timeline_relationship")
                feedback_parts.append(evidence_model_text(evidence["evidence_id"], evidence["cache_label"], clip_text(text, 5000)))
                continue

            if name == "timeline_map":
                metrics["timeline_cache_calls"] += 1
                payload = timeline_map(
                    case_id=arguments.get("case_id") or None,
                    entity=arguments.get("entity") or None,
                    limit=arguments.get("limit") or 100,
                )
                if payload.get("events"):
                    metrics["timeline_cache_hits"] += 1
                evidence, text = _timeline_result_evidence(case_state, name, arguments, payload, "timeline_map")
                feedback_parts.append(evidence_model_text(evidence["evidence_id"], evidence["cache_label"], clip_text(text, MAX_TOTAL_MODEL_RESULT_CHARS)))
                continue

            if name == "web_search":
                metrics["external_calls"] += 1
                result_text = await asyncio.to_thread(web_search_public, arguments.get("query", ""))
                compacted = clip_text(result_text, MAX_TOTAL_MODEL_RESULT_CHARS)
                evidence = record_evidence(
                    case_state, "external", name, compacted,
                    query=arguments.get("query", ""),
                    signature=stable_hash({"tool": name, "query": arguments.get("query", "")}),
                    cache_label="fresh external work",
                    method_class="external_search",
                )
                feedback_parts.append(evidence_model_text(evidence["evidence_id"], evidence["cache_label"], compacted))
                continue

            if name == "fetch_url":
                metrics["external_calls"] += 1
                result_text = await asyncio.to_thread(fetch_public_url, arguments.get("url", ""))
                compacted = clip_text(result_text, MAX_TOTAL_MODEL_RESULT_CHARS)
                evidence = record_evidence(
                    case_state, "external", name, compacted,
                    query=arguments.get("url", ""),
                    signature=stable_hash({"tool": name, "url": arguments.get("url", "")}),
                    cache_label="fresh external work",
                    method_class="external_fetch",
                )
                feedback_parts.append(evidence_model_text(evidence["evidence_id"], evidence["cache_label"], compacted))
                continue

            phase = _current_phase(plan)
            model_result, evidence, guarded_args = await execute_splunk_search(
                session=session,
                tool_name=name,
                arguments=arguments,
                case_state=case_state,
                metrics=metrics,
                round_number=round_number,
                active_skills=active_skills,
                phase=phase,
                question=question,
            )
            feedback_parts.append(clip_text(model_result, 2600))

            fatal_stop = _stop_for_fatal_tool_error(case_state, metrics, active_skills, plan)
            if fatal_stop is not None:
                return fatal_stop

            if evidence:
                if phase == "DO":
                    update_plan_phase(plan, "DO", "completed", "Primary evidence search executed.", [evidence["evidence_id"]])
                    update_plan_phase(plan, "CHECK", "in_progress", "Cross-check with a different method.")
                    if "bidirectional_timeframe" not in active_skills:
                        complete_next_todo(plan, [evidence["evidence_id"]], "Primary evidence completed.")
                elif phase == "CHECK":
                    update_plan_phase(plan, "CHECK", "completed", "Independent check evidence obtained.", [evidence["evidence_id"]])
                    if "bidirectional_timeframe" not in active_skills:
                        complete_next_todo(plan, [evidence["evidence_id"]], "Cross-check evidence completed.")

                direct_result = await _attempt_direct_extraction_release(
                    session=session, question=question, query_strategy=query_strategy,
                    case_state=case_state, metrics=metrics, active_skills=active_skills,
                    plan=plan, reasoner=reasoner,
                )
                if direct_result is not None:
                    return direct_result

                if "bidirectional_timeframe" in active_skills and not current_anchor(case_state.get("case_id")) and case_state.get("timeline_rows", 0):
                    feedback_parts.append(
                        "Timeline events are now mapped but T0 is not set. Choose an evidence-backed event and call "
                        "set_timeline_anchor before interpreting BEFORE/AFTER chronology."
                    )

        metrics["evidence_items"] = len(case_state["evidence"])
        metrics["timeline_rows"] = case_state.get("timeline_rows", 0)
        feedback = "\n\n".join(feedback_parts[-4:])

    print("\n" + "=" * 70)
    print("STOPPED")
    print("=" * 70)
    print(f"Agent reached the maximum of {MAX_AGENT_ROUNDS} rounds.")
    metrics["final_validation_status"] = "round_limit"
    metrics["evidence_items"] = len(case_state["evidence"])
    metrics["timeline_rows"] = case_state.get("timeline_rows", 0)
    if last_candidate:
        print("Returning last candidate with limited validation.")
        print_observation_summary(metrics, active_skills)
        return last_candidate, metrics, active_skills, plan
    print_observation_summary(metrics, active_skills)
    return None, metrics, active_skills, plan
