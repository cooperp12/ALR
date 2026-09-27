import json
import sys
import time
import traceback
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from commander_agent.config import *
from commander_agent.core.preflight import ollama_preflight, local_preflight, splunk_search_preflight
from commander_agent.core.environment import make_environment
from commander_agent.skills.registry import BASELINE_SKILLS, select_skills
from commander_agent.skills.strategy import compose_query_strategy, strategy_prompt_text
from commander_agent.mcp.adapter import mcp_tools_to_ollama
from commander_agent.state.cache import get_validated_answer, invalidate_validated_answer
from commander_agent.state.evidence import prior_validated_context, append_eval_run
from commander_agent.state.timeline import timeline_context_for_question
from commander_agent.state.plan import build_plan, print_plan
from commander_agent.core.investigate import investigate
from commander_agent.evaluation.scorer import score_final_answer, load_lab_tests
from commander_agent.reasoning.broker import ReasoningBroker
from commander_agent.semantic.frame import build_investigation_frame, render_investigation_frame
from commander_agent.semantic.binding import resolve_semantic_bindings, compact_bindings
from commander_agent.core.q6_ubuntu_launch import resolve_q6_from_sequence
from commander_agent.state.sequence import build_validated_sequence_state as build_sequence_state, render_validated_sequence_state as render_sequence_state
from commander_agent.evaluation.isolation import prepare_benchmark_isolation


def _empty_metrics():
    keys = [
        "rounds_used","tool_calls","search_attempts","search_calls","query_cache_hits","current_run_cache_hits",
        "guardrail_blocks","skill_contract_blocks","retry_diversity_blocks","structured_output_rejections","zero_results",
        "tool_errors","fatal_tool_errors","external_calls","timeline_cache_calls","timeline_cache_hits","timeline_anchor_sets",
        "timeline_before_calls","timeline_after_calls","timeline_relationship_calls","map_first_reuse_checks","map_first_reuse_hits",
        "evidence_items","observations_stored","timeline_rows","candidate_answers","validation_reviews",
        "measurement_resolution_calls","measurement_resolution_failures","measurement_resolution_unresolved",
        "measurement_contracts_created","deterministic_candidate_derivations","measurement_drift_blocks",
        "investigation_frame_calls","investigation_frame_failures","semantic_bindings_created","semantic_binding_contract_promotions",
        "semantic_binding_invalidations","semantic_conflicts_detected","semantic_binding_query_blocks","semantic_query_policy_blocks",
        "multivalue_cardinality_blocks","ranking_population_blocks","source_contract_blocks","extraction_contract_blocks",
        "discovery_specificity_blocks","extraction_contracts_created","direct_extraction_derivations","direct_extraction_verifications",
        "supervisor_query_reviews","supervisor_relationship_review_failures","bootstrap_queries","deterministic_resolutions",
        "deterministic_validation_blocks","semantic_skill_reviews","semantic_skill_failures","semantic_skill_query_actions",
        "semantic_skill_accepts","semantic_validation_blocks","semantic_format_repairs","semantic_format_repair_successes",
        "semantic_action_contract_rejections","semantic_action_repairs","semantic_action_repair_successes","scope_guard_reviews",
        "scope_guard_allows","scope_guard_denials","scope_guard_failures","scope_guard_blocks","supervisor_reviews",
        "supervisor_failures","supervisor_fallbacks","supervisor_redirects","supervisor_stop_allows","supervisor_stop_denials",
        "supervisor_validation_allows","canonical_release_allows","supervisor_eligible_snapshots","temporal_analysis_calls",
        "temporal_analysis_rows","temporal_analysis_groups","temporal_graphs","semantic_stagnation_blocks","candidate_quarantines",
        "agent_no_evidence_blocks","evidence_version","context_compactions","max_estimated_prompt_tokens","model_router_calls",
        "heuristic_router_hits","model_planner_calls","deterministic_planner_hits","ollama_retries","ollama_truncation_detected",
        "ollama_truncation_retries","ollama_investigator_prompt_tokens","ollama_investigator_output_tokens",
        "ollama_supervisor_prompt_tokens","ollama_supervisor_output_tokens","remote_candidate_fastpath","openai_reasoning_calls",
        "openai_reasoning_failures","openai_input_tokens","openai_output_tokens","openai_total_tokens",
        "q6_sequence_state_reuses","q6_first_event_resolutions","q6_first_event_verifications",
        "q6_enrichment_calls","q6_enrichment_resolutions",
        "q6_codename_enrichment_calls","q6_codename_enrichment_resolutions",
        "q6_timeline_events","q6_timeline_relationship_pivots","q6_timeline_first_selections",
        "q6_temporal_scope_expansions","q6_identity_expansions","q6_related_access_keys","benchmark_state_archives",
    ]
    out = {k: 0 for k in keys}
    out["queries"] = []
    out["final_validation_status"] = "not reached"
    return out


def _runtime_splunk_credentials():
    username = (SPLUNK_USERNAME or "").strip()
    password = SPLUNK_PASSWORD or ""
    return (username, password, "config/credentials.json or environment") if username and password else (username, password, "missing")


def _benchmark_questions():
    tests, _ = load_lab_tests()
    if not tests:
        return []
    order = ("Q1", "Q2", "Q3", "Q6")
    return [(qid, tests[qid]["question"]) for qid in order if qid in tests]



async def _run_question(
    session, reasoner, ollama_tools, question, *, benchmark_mode=False, label=None,
    sequence_context="", sequence_state=None, forced_skills=None, qid=None,
):
    if label:
        print("\n" + "#" * 70)
        print(f"ALR legacy compatibility BENCHMARK {label}")
        print("#" * 70)
        print(question)

    metrics = _empty_metrics()
    if not benchmark_mode:
        validated_hit = get_validated_answer(question)
        if validated_hit:
            answer = validated_hit.get("answer", "")
            print("\n" + "=" * 70 + "\nVALIDATED WORK CACHE HIT\n" + "=" * 70)
            print(answer)
            metrics["final_validation_status"] = "validated_cache_hit"
            skills = validated_hit.get("skills", [])
            passed = score_final_answer(question, answer, metrics, skills)
            if passed is False:
                invalidate_validated_answer(question, "Independent lab scorer rejected cache entry.")
            append_eval_run(question, answer, metrics, skills, passed)
            return {"qid": qid, "question": question, "answer": answer, "metrics": metrics, "skills": skills, "passed": passed}

    prior_context = "" if benchmark_mode else prior_validated_context(question)
    if sequence_state:
        sequence_context = render_sequence_state(sequence_state)
        forced_skills = list(forced_skills or []) + (["incident_sequence_state"] if "incident_sequence_state" not in (forced_skills or []) else [])
    if sequence_context:
        prior_context = (sequence_context + ("\n\n" + prior_context if prior_context else "")).strip()
    timeline_context = "" if benchmark_mode else timeline_context_for_question(question)
    if timeline_context:
        metrics["timeline_cache_hits"] += 1
        print("Reusable timeline/case-map context found.")

    frame = build_investigation_frame(question, metrics=metrics)
    bindings = resolve_semantic_bindings(frame, question=question)
    metrics["semantic_bindings_created"] = len(bindings)
    print("\nInvestigation Frame:")
    print(render_investigation_frame(frame))
    print("\nSemantic bindings (pre-query; result values hidden):")
    print(compact_bindings(bindings) or "[none]")

    routed = select_skills(question, metrics=metrics, reasoner=reasoner)
    initial_skills = BASELINE_SKILLS + [s for s in routed if s not in BASELINE_SKILLS]
    for skill in forced_skills or []:
        if skill not in initial_skills:
            initial_skills.append(skill)
    print("\nSkills: " + ", ".join(initial_skills))

    query_strategy = compose_query_strategy(question, initial_skills, investigation_frame=frame, semantic_bindings=bindings)
    print("\nSkill strategy:")
    print(strategy_prompt_text(query_strategy))
    source_contract = query_strategy.get("source_contract") or {}
    extraction_contract = query_strategy.get("extraction_contract") or {}
    if source_contract:
        print("\nSource contract:")
        print(json.dumps(source_contract, indent=2, ensure_ascii=False))
    if extraction_contract:
        print("\nExtraction contract:")
        print(json.dumps(extraction_contract, indent=2, ensure_ascii=False))

    plan = build_plan(
        question, initial_skills, prior_context, query_strategy=query_strategy, metrics=metrics,
        reasoner=reasoner, investigation_frame=frame, semantic_bindings=bindings,
    )
    if benchmark_mode:
        plan["bypass_persistent_cache"] = True
    print_plan(plan)

    start = time.time()
    answer, metrics, used_skills, final_plan = await investigate(
        session, ollama_tools, question, initial_skills, plan, prior_context,
        query_strategy=query_strategy, seed_metrics=metrics, reasoner=reasoner,
    )
    metrics["elapsed_seconds"] = round(time.time() - start, 3)
    print_plan(final_plan)

    passed = None
    if answer is not None:
        passed = score_final_answer(question, answer, metrics, used_skills)
        if passed is False and metrics.get("final_validation_status") == "validated":
            invalidate_validated_answer(question, "Independent lab scorer rejected validated answer.")
            print("[Eval feedback] Invalidated rejected validated-cache entry.")
    append_eval_run(question, answer, metrics, used_skills, passed)
    return {"qid": qid, "question": question, "answer": answer, "metrics": metrics, "skills": used_skills, "passed": passed}


async def _run_q6_sequence_case(session, reasoner, ollama_tools, question, sequence_state):
    print("\n" + "#" * 70)
    print("ALR legacy compatibility SEQUENCE Q6 (4/4)")
    print("#" * 70)
    print(question)
    print("\nIncident sequence state:")
    print(render_sequence_state(sequence_state))

    metrics = _empty_metrics()
    skills = BASELINE_SKILLS + [
        "aws_cloudtrail",
        "bidirectional_timeframe",
        "chronological_first_event",
        "incident_sequence_state",
        "cloudtrail_identity_expansion",
        "ami_release_resolution",
        "external_enrichment",
    ]
    print("\nQ6 deterministic skills: " + ", ".join(skills))
    print("Q6 contract: validated Q3 key -> shared timeline identity expansion -> first related RunInstances -> independent first-event check -> exact AMI release evidence -> official Ubuntu codename evidence")

    start = time.time()
    resolution = await resolve_q6_from_sequence(session, sequence_state, metrics=metrics)
    metrics["elapsed_seconds"] = round(time.time() - start, 3)

    if resolution.get("status") == "validated":
        first = resolution.get("first_attempt") or {}
        enrichment = resolution.get("enrichment") or {}
        release = enrichment.get("release") or {}
        print("\nDeterministic Q6 evidence chain:")
        print(f"  access key : {resolution.get('access_key', '')}")
        print(f"  first time : {first.get('event_time', '')}")
        print(f"  IAM user   : {first.get('iam_user', '')}")
        print(f"  region     : {first.get('region', '')}")
        print(f"  AMI        : {first.get('image_id', '')}")
        print(f"  enrichment : {enrichment.get('source', '')}")
        print(f"  release    : {release.get('version', '')} / {release.get('series', '')}")
        answer = resolution.get("answer")
        print("\nFINAL ANSWER:")
        print(answer)
        passed = score_final_answer(question, answer, metrics, skills)
        append_eval_run(question, answer, metrics, skills, passed)
        return {"qid": "Q6", "question": question, "answer": answer, "metrics": metrics, "skills": skills, "passed": passed}

    print("\n[Q6 deterministic resolver did not reach release]")
    print(f"Stage : {resolution.get('stage', 'unknown')}")
    print(f"Reason: {resolution.get('reason', 'No reason supplied')}")
    print("Q6 stopped at the missing evidence step. Diagnostics: ALR_q6_resolution.jsonl + ALR_q6_stage_trace.json")
    print("The bounded recovery stays in CloudTrail; no email-source restart is performed.")
    append_eval_run(question, None, metrics, skills, None)
    return {"qid": "Q6", "question": question, "answer": None, "metrics": metrics, "skills": skills, "passed": None}


def _print_benchmark_summary(results):
    print("\n" + "=" * 70)
    print("ALR legacy compatibility Q1-Q3 -> Q6 SEQUENCE SUMMARY")
    print("=" * 70)
    rows = []
    for i, r in enumerate(results, 1):
        m = r.get("metrics") or {}
        status = "PASS" if r.get("passed") is True else ("CHECK" if r.get("answer") else "NO ANSWER")
        qid = r.get("qid") or f"Q{i}"
        answer = r.get("answer") or "[none]"
        print(f"{qid}: {status:9} | answer={answer} | validation={m.get('final_validation_status')} | searches={m.get('search_calls')} | elapsed={m.get('elapsed_seconds')}s")
        rows.append({
            "qid": qid, "passed": r.get("passed"), "answer": r.get("answer"),
            "final_validation_status": m.get("final_validation_status"),
            "search_calls": m.get("search_calls"), "search_attempts": m.get("search_attempts"),
            "tool_calls": m.get("tool_calls"), "elapsed_seconds": m.get("elapsed_seconds"),
            "ollama_prompt_tokens": m.get("ollama_prompt_tokens", 0), "ollama_output_tokens": m.get("ollama_output_tokens", 0),
            "supervisor_reviews": m.get("supervisor_reviews", 0), "canonical_release_allows": m.get("canonical_release_allows", 0),
        })
    passed_count = sum(1 for r in results if r.get("passed") is True)
    total_elapsed = round(sum(float((r.get("metrics") or {}).get("elapsed_seconds") or 0) for r in results), 3)
    total_searches = sum(int((r.get("metrics") or {}).get("search_calls") or 0) for r in results)
    total_prompt = sum(int((r.get("metrics") or {}).get("ollama_prompt_tokens") or 0) for r in results)
    total_output = sum(int((r.get("metrics") or {}).get("ollama_output_tokens") or 0) for r in results)
    print(f"\nCombined score : {passed_count}/{len(results)}")
    print(f"Splunk searches: {total_searches}")
    print(f"Elapsed total  : {total_elapsed}s")
    print(f"Ollama tokens  : prompt={total_prompt}, output={total_output}")
    summary_path = Path("ALR_sequence_summary.json")
    summary_path.write_text(json.dumps({
        "score": f"{passed_count}/{len(results)}",
        "totals": {"splunk_searches": total_searches, "elapsed_seconds": total_elapsed, "ollama_prompt_tokens": total_prompt, "ollama_output_tokens": total_output},
        "runs": rows
    }, indent=2), encoding="utf-8")
    print(f"Summary file   : {summary_path}")


async def main():
    print("=" * 70)
    print("BOTSv3 OLLAMA + SPLUNK MCP - ALR legacy compatibility INCIDENT SEQUENCE + Q6 AMI RESOLUTION")
    print("=" * 70)
    print(f"Investigator: {INVESTIGATOR_MODEL} (ctx={INVESTIGATOR_NUM_CTX}, think={INVESTIGATOR_THINK})")
    print(f"Supervisor  : {SUPERVISOR_MODEL} (ctx={SUPERVISOR_NUM_CTX}, think={SUPERVISOR_THINK})")
    print(f"Role unload : {OLLAMA_ROLE_KEEP_ALIVE} keep_alive (memory-safe model switching)")
    print(f"Splunk host : {SPLUNK_HOST}:{SPLUNK_PORT}")
    print(f"Credentials : {CREDENTIALS_FILE}")
    print("Architecture: Q1-Q3 validated state -> chronological RunInstances -> AMI enrichment -> canonical release")

    ok, problems = local_preflight()
    if not ok:
        print("\nPREFLIGHT FAILED")
        for p in problems: print(" - " + p)
        return
    runtime_username, runtime_password, credential_source = _runtime_splunk_credentials()
    if not runtime_username or not runtime_password:
        print("\nPREFLIGHT FAILED")
        print(f" - Splunk credentials are missing from: {CREDENTIALS_FILE}")
        return
    print(f"Splunk credential source: {credential_source} (values not logged)")
    ok, msg = ollama_preflight(); print("\nOllama preflight: " + msg)
    if not ok: return

    server = StdioServerParameters(command=MCP_PYTHON, args=[MCP_SERVER], cwd=MCP_CWD, env=make_environment(splunk_username=runtime_username, splunk_password=runtime_password))
    try:
        async with stdio_client(server, errlog=sys.stderr) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                print("\nInitialising Splunk MCP...")
                await session.initialize(); print("Splunk MCP transport: CONNECTED")
                search_ok, search_info = await splunk_search_preflight(session)
                if not search_ok:
                    print("\nSPLUNK SEARCH PREFLIGHT FAILED")
                    print(search_info.get("message", "Unknown Splunk search failure")); return
                print("Splunk search preflight: PASS - BOTSv3 searchable." + (f" count={search_info.get('botsv3_count')}" if search_info.get('botsv3_count') is not None else ""))
                reasoner = ReasoningBroker(); remote_ok, remote_msg = reasoner.preflight(); print("OpenAI reasoning preflight: " + remote_msg)
                print("OpenAI reasoning mode: " + ("ENABLED" if remote_ok else "DISABLED for this run; local dual-model roles only."))
                tools_response = await session.list_tools(); ollama_tools = mcp_tools_to_ollama(tools_response.tools)

                print("\nInput options:")
                print("  1  = run solved Q1 -> Q2 -> Q3, then solve Q6 using their validated incident state")
                print("  Or paste any BOTSv3 question to run it once")
                choice = input("\n> ").strip()
                if not choice:
                    print("No input entered."); return
                if choice == "1":
                    isolation = prepare_benchmark_isolation()
                    print(f"Benchmark isolation: fresh state; archived={len(isolation.get('archived_items', []))}")
                    cases = _benchmark_questions()
                    case_map = dict(cases)
                    if tuple(case_map) != ("Q1", "Q2", "Q3", "Q6"):
                        print("Benchmark questions unavailable; packaged benchmark must contain Q1, Q2, Q3 and Q6."); return
                    results = []
                    for idx, qid in enumerate(("Q1", "Q2", "Q3"), 1):
                        result = await _run_question(
                            session, reasoner, ollama_tools, case_map[qid], benchmark_mode=True,
                            label=f"{qid} ({idx}/4)", qid=qid,
                        )
                        results.append(result)
                    sequence_state = build_sequence_state(results)
                    if "compromised_access_key" not in sequence_state:
                        print("\nQ6 cannot start: Q3 did not yield a validated compromised access key.")
                    else:
                        results.append(await _run_q6_sequence_case(
                            session, reasoner, ollama_tools, case_map["Q6"], sequence_state
                        ))
                    _print_benchmark_summary(results)
                else:
                    await _run_question(session, reasoner, ollama_tools, choice, benchmark_mode=False)
    except BaseException as exc:
        print("\n" + "=" * 70 + "\nERROR\n" + "=" * 70)
        traceback.print_exception(type(exc), exc, exc.__traceback__)
