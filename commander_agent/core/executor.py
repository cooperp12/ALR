import json
from pathlib import Path

from commander_agent.config import MAX_SEARCH_EXECUTIONS, QUERY_FAILURE_LEDGER_FILE
from commander_agent.mcp.guardrails import apply_search_guardrail
from commander_agent.mcp.adapter import tool_result_to_text
from commander_agent.mcp.logging import append_full_result_log
from commander_agent.mcp.results import compact_search_result
from commander_agent.mcp.query_shape import semantic_query_fingerprint
from commander_agent.skills.contracts import enforce_query_contracts, query_method_class
from commander_agent.skills.structured_output_validation.scripts.schema import (
    validate_search_arguments,
    validate_search_result_text,
)
from commander_agent.state.cache import (
    query_cache_signature,
    get_query_cache_entry,
    bump_query_cache_hit,
    put_query_cache_entry,
    result_event_count,
)
from commander_agent.state.evidence import (
    record_evidence,
    record_structured_finding,
    evidence_model_text,
)
from commander_agent.state.evidence_store import ingest_machine_evidence
from commander_agent.semantic.conflicts import detect_and_rebind
from commander_agent.semantic.query_policy import semantic_query_policy_violations
from commander_agent.skills.investigation_supervisor.scripts.reviewer import review_investigation_progress
from commander_agent.state.io import append_jsonl
from commander_agent.state.scope import build_scope_contract, scope_violations



def _supervisor_relationship_review(question, case_state, metrics, *, query, phase, status, violations=None, evidence=None):
    """Give the Supervisor an active review role without allowing it to bypass Python invariants."""
    try:
        review = review_investigation_progress(
            question=question,
            case_state=case_state,
            metrics=metrics,
            last_action={
                "action": "splunk_query",
                "phase": phase,
                "query": query,
            },
            last_result={
                "status": status,
                "violations": list(violations or []),
                "evidence_id": (evidence or {}).get("evidence_id") if isinstance(evidence, dict) else None,
                "method_class": (evidence or {}).get("method_class") if isinstance(evidence, dict) else None,
            },
            candidate="",
        )
        case_state["latest_supervisor_relationship_review"] = review
        metrics["supervisor_query_reviews"] = metrics.get("supervisor_query_reviews", 0) + 1
        return review
    except Exception as exc:
        # review_investigation_progress is already fail-safe, but execution must never
        # depend on a model review being available. Python invariants remain authoritative.
        metrics["supervisor_relationship_review_failures"] = metrics.get("supervisor_relationship_review_failures", 0) + 1
        return {
            "evaluation": "BLOCKED" if status == "blocked" else "PARTIAL",
            "allow_validation": False,
            "allow_stop": False,
            "reason": f"Supervisor relationship review unavailable: {type(exc).__name__}: {exc}",
        }


def _compact_supervisor_feedback(review):
    if not isinstance(review, dict):
        return ""
    return json.dumps({
        "evaluation": review.get("evaluation"),
        "remaining_gap": review.get("remaining_gap"),
        "next_capabilities": review.get("next_capabilities") or [],
        "invalidate_from_stage": review.get("invalidate_from_stage"),
        "allow_validation": bool(review.get("allow_validation")),
        "allow_stop": bool(review.get("allow_stop")),
        "reason": review.get("reason"),
    }, ensure_ascii=False)

def _failure_record(case_state, tool_name, arguments, kind, errors, round_number, failure=None):
    record = {
        "record_type": "query_failure",
        "case_id": case_state.get("case_id"),
        "round": round_number,
        "tool": tool_name,
        "arguments": arguments,
        "kind": kind,
        "errors": list(errors or []),
        "failure": failure or {},
    }
    append_jsonl(QUERY_FAILURE_LEDGER_FILE, record)
    return record


async def execute_splunk_search(
    session,
    tool_name,
    arguments,
    case_state,
    metrics,
    round_number,
    active_skills,
    phase,
    question,
    allow_scope_change=False,
):
    metrics["search_attempts"] += 1
    guarded_args, blocked = apply_search_guardrail(tool_name, arguments)

    args_ok, canonical_args, arg_errors = validate_search_arguments(guarded_args)
    if not args_ok:
        metrics["guardrail_blocks"] += 1
        metrics["structured_output_rejections"] = metrics.get("structured_output_rejections", 0) + 1
        _failure_record(case_state, tool_name, guarded_args, "bad_query_object", arg_errors, round_number)
        return (
            json.dumps({
                "guardrail": "Structured query object rejected before execution.",
                "errors": arg_errors,
                "required_action": "Return a valid JSON tool object with a non-empty string query and correctly typed parameters.",
            }, indent=2),
            None,
            guarded_args,
        )
    guarded_args = canonical_args
    query = guarded_args.get("query", "")

    if query:
        metrics["queries"].append(query)
        scope_contract = case_state.get("scope_contract") or {}
        if scope_contract.get("base_scope_spl") and not allow_scope_change:
            invariant_errors = scope_violations(query, scope_contract)
            if invariant_errors:
                metrics["guardrail_blocks"] += 1
                metrics["scope_guard_blocks"] = metrics.get("scope_guard_blocks", 0) + 1
                return (
                    json.dumps({
                        "guardrail": "Query scope invariant blocked this search.",
                        "violations": invariant_errors,
                        "required_action": (
                            "Preserve the current scope and change only the measurement, or use "
                            "the semantic scope-change review path with an explicit justification."
                        ),
                    }, indent=2),
                    None,
                    guarded_args,
                )

    if blocked:
        metrics["guardrail_blocks"] += 1
        return blocked, None, guarded_args

    # ALR legacy compatibility universal semantic relationship gate. This runs before cache lookup and
    # before every MCP execution path, so a free-form Investigator cannot bypass a
    # semantic binding or active measurement contract. Python blocks mechanically;
    # the Supervisor reviews the conflict and decides whether semantics should reopen.
    policy_violations = semantic_query_policy_violations(query, case_state, phase=phase)
    if policy_violations:
        metrics["guardrail_blocks"] += 1
        metrics["semantic_query_policy_blocks"] = metrics.get("semantic_query_policy_blocks", 0) + 1
        codes = {v.get("code") for v in policy_violations}
        if "SEMANTIC_BINDING_QUERY_MISMATCH" in codes:
            metrics["semantic_binding_query_blocks"] = metrics.get("semantic_binding_query_blocks", 0) + 1
        if "MEASUREMENT_CONTRACT_VIOLATION" in codes:
            metrics["measurement_drift_blocks"] = metrics.get("measurement_drift_blocks", 0) + 1
        if "MULTIVALUE_LEN_CARDINALITY_ERROR" in codes:
            metrics["multivalue_cardinality_blocks"] = metrics.get("multivalue_cardinality_blocks", 0) + 1
        if "INCOMPLETE_RANKING_POPULATION" in codes:
            metrics["ranking_population_blocks"] = metrics.get("ranking_population_blocks", 0) + 1
        if "SOURCE_CONTRACT_MISMATCH" in codes:
            metrics["source_contract_blocks"] = metrics.get("source_contract_blocks", 0) + 1
        if "EXTRACTION_OUTPUT_FIELD_MISSING" in codes or "EXTRACTION_ENTITY_FILTER_MISSING" in codes:
            metrics["extraction_contract_blocks"] = metrics.get("extraction_contract_blocks", 0) + 1
        if "UNOBSERVED_EVENT_SPECIFICITY" in codes:
            metrics["discovery_specificity_blocks"] = metrics.get("discovery_specificity_blocks", 0) + 1
        supervisor = _supervisor_relationship_review(
            question, case_state, metrics, query=query, phase=phase, status="blocked",
            violations=policy_violations,
        )
        return (
            json.dumps({
                "guardrail": "ALR legacy compatibility semantic relationship policy blocked this search before Splunk execution.",
                "violations": policy_violations,
                "supervisor_review": supervisor,
                "required_action": (
                    "Repair the SPL to satisfy the active semantic relationships. If the relationship itself is wrong, "
                    "use supervised semantic recovery to explicitly invalidate/reopen it before querying."
                ),
            }, ensure_ascii=False, indent=2),
            None,
            guarded_args,
        )

    contract_block = enforce_query_contracts(query, active_skills, phase=phase)
    if contract_block:
        metrics["guardrail_blocks"] += 1
        metrics["skill_contract_blocks"] += 1
        return json.dumps(contract_block, indent=2), None, guarded_args

    shape = semantic_query_fingerprint(query)
    if shape in case_state.get("failed_query_shapes", set()):
        metrics["retry_diversity_blocks"] += 1
        metrics["guardrail_blocks"] += 1
        return (
            json.dumps({
                "guardrail": "Retry diversity rule blocked a semantic repeat of a prior failed/non-finding query.",
                "required_action": (
                    "Change field, sourcetype, predicate, aggregation, or time strategy. "
                    "Do not cosmetically rewrite the same failed search."
                ),
            }, indent=2),
            None,
            guarded_args,
        )

    signature = query_cache_signature(tool_name, guarded_args)
    method_class = query_method_class(query)

    current_id = case_state["signature_to_evidence"].get(signature)
    if current_id:
        metrics["current_run_cache_hits"] += 1
        existing = next((r for r in case_state["evidence"] if r.get("evidence_id") == current_id), None)
        if existing:
            cached = existing.get("result_excerpt", "")
            return (
                evidence_model_text(
                    current_id,
                    "current-run cached work",
                    cached,
                    "Exact search already executed. Reuse it as WORK; it does not count as independent validation.",
                ),
                existing,
                guarded_args,
            )

    cached_entry = None if case_state.get("bypass_persistent_cache") else get_query_cache_entry(signature)
    if cached_entry:
        metrics["query_cache_hits"] += 1
        bump_query_cache_hit(signature)
        cached_model = cached_entry.get("model_result", "{}")
        evidence = record_evidence(
            case_state,
            "splunk",
            tool_name,
            cached_model,
            query=query,
            signature=signature,
            cache_label="persistent cached work",
            method_class=method_class,
        )
        evidence["result_limit"] = guarded_args.get("max_count", 50)
        try:
            evidence["machine_result"] = json.loads(cached_model)
        except (TypeError, ValueError):
            evidence["machine_result"] = {}
        new_observations = ingest_machine_evidence(case_state, evidence, evidence["machine_result"])
        metrics["observations_stored"] = metrics.get("observations_stored", 0) + len(new_observations)
        detect_and_rebind(case_state, evidence, evidence["machine_result"], metrics=metrics)
        supervisor = _supervisor_relationship_review(
            question, case_state, metrics, query=query, phase=phase, status="evidence_reused", evidence=evidence
        )
        if cached_entry.get("event_count") == 0:
            case_state.setdefault("failed_query_shapes", set()).add(shape)
        return (
            evidence_model_text(
                evidence["evidence_id"],
                evidence["cache_label"],
                cached_model,
                "Persistent query-cache hit: Splunk execution skipped. Cached work still requires independent validation. "
                "Supervisor relationship review: " + _compact_supervisor_feedback(supervisor),
            ),
            evidence,
            guarded_args,
        )

    if metrics["search_calls"] >= MAX_SEARCH_EXECUTIONS:
        metrics["guardrail_blocks"] += 1
        return (
            json.dumps({
                "guardrail": "Search execution budget reached.",
                "limit": MAX_SEARCH_EXECUTIONS,
                "required_action": "Review existing evidence and return the best-supported candidate.",
            }, indent=2),
            None,
            guarded_args,
        )

    metrics["search_calls"] += 1
    try:
        result = await session.call_tool(tool_name, arguments=guarded_args)
        result_text = tool_result_to_text(result)
    except Exception as exc:
        metrics["tool_errors"] += 1
        result_text = json.dumps({
            "error": "MCP tool execution failed",
            "details": f"{type(exc).__name__}: {exc}",
        })

    append_full_result_log(round_number, tool_name, guarded_args, result_text)

    # Critical ALR legacy compatibility change: failed/malformed result objects are NEVER promoted
    # to evidence/query cache. They become failure telemetry and retry-diversity state.
    validation = validate_search_result_text(result_text)
    if not validation.get("ok"):
        metrics["tool_errors"] += 1
        metrics["structured_output_rejections"] = metrics.get("structured_output_rejections", 0) + 1
        case_state.setdefault("failed_query_shapes", set()).add(shape)
        failure = validation.get("failure") or {}
        _failure_record(
            case_state,
            tool_name,
            guarded_args,
            validation.get("kind", "invalid_result"),
            validation.get("errors", []),
            round_number,
            failure=failure,
        )
        if failure.get("fatal"):
            case_state["fatal_tool_error"] = {
                "kind": failure.get("kind", validation.get("kind", "tool_error")),
                "code": failure.get("code", "SPLUNK_FATAL_ERROR"),
                "message": failure.get("message") or "; ".join(validation.get("errors", [])),
                "layer": failure.get("layer", "splunk"),
                "retryable": bool(failure.get("retryable", False)),
                "round": round_number,
            }
            metrics["fatal_tool_errors"] = metrics.get("fatal_tool_errors", 0) + 1
        canonical = validation.get("payload") or {
            "error": validation.get("kind", "invalid_result"),
            "details": validation.get("errors", []),
        }
        if failure:
            canonical = dict(canonical)
            canonical["classified_failure"] = failure
        return (
            json.dumps(canonical, ensure_ascii=False, indent=2),
            None,
            guarded_args,
        )

    # Canonicalise the validated payload before compaction, ensuring compact_search_result
    # never receives malformed JSON that could later be cached.
    validated_text = json.dumps(validation["payload"], ensure_ascii=False)
    compacted = compact_search_result(validated_text)
    compact_validation = validate_search_result_text(compacted)
    if not compact_validation.get("ok"):
        metrics["tool_errors"] += 1
        metrics["structured_output_rejections"] = metrics.get("structured_output_rejections", 0) + 1
        case_state.setdefault("failed_query_shapes", set()).add(shape)
        _failure_record(
            case_state,
            tool_name,
            guarded_args,
            "compaction_validation_failed",
            compact_validation.get("errors", []),
            round_number,
        )
        return json.dumps({"error": "Validated result failed compacted JSON contract."}, indent=2), None, guarded_args

    event_count = result_event_count(compacted)
    if event_count == 0:
        metrics["zero_results"] += 1
        case_state.setdefault("failed_query_shapes", set()).add(shape)

    cached, cache_error = put_query_cache_entry(signature, tool_name, guarded_args, compacted)
    if not cached:
        metrics["structured_output_rejections"] = metrics.get("structured_output_rejections", 0) + 1
        print(f"[Query cache rejected result: {cache_error}]")

    evidence = record_evidence(
        case_state,
        "splunk",
        tool_name,
        compacted,
        query=query,
        signature=signature,
        cache_label="fresh work",
        method_class=method_class,
    )
    evidence["machine_result"] = validation["payload"]
    evidence["result_limit"] = guarded_args.get("max_count", 50)
    new_observations = ingest_machine_evidence(case_state, evidence, validation["payload"])
    metrics["observations_stored"] = metrics.get("observations_stored", 0) + len(new_observations)
    detect_and_rebind(case_state, evidence, validation["payload"], metrics=metrics)
    append_jsonl(Path(QUERY_FAILURE_LEDGER_FILE).parent / "machine-results.jsonl", {"case_id": case_state.get("case_id"), "evidence_id": evidence["evidence_id"], "query": query, "result_limit": evidence["result_limit"], "result": validation["payload"]})
    if not (case_state.get("scope_contract") or {}).get("base_scope_spl"):
        case_state["scope_contract"] = build_scope_contract(query)

    supervisor = _supervisor_relationship_review(
        question, case_state, metrics, query=query, phase=phase, status="evidence_recorded", evidence=evidence
    )
    extra = f"FRESH WORK via {method_class or 'search'}. Use a materially different method for CHECK."
    extra += " Supervisor relationship review: " + _compact_supervisor_feedback(supervisor)
    if event_count == 0:
        extra += " Non-finding: retry only with a changed search strategy."

    return (
        evidence_model_text(evidence["evidence_id"], evidence["cache_label"], compacted, extra),
        evidence,
        guarded_args,
    )


async def execute_bootstrap_strategy(
    session,
    strategy,
    case_state,
    metrics,
    active_skills,
    question,
):
    evidence_ids = []
    outputs = []

    for label, phase, query in (
        ("PRIMARY", "DO", strategy.get("primary_query")),
        ("CHECK", "CHECK", strategy.get("check_query")),
    ):
        if not query:
            continue
        print("\n" + "-" * 70)
        print(f"ALR legacy compatibility SKILL BOOTSTRAP: {label}")
        print("-" * 70)
        print(query)

        metrics["bootstrap_queries"] += 1
        model_result, evidence, guarded = await execute_splunk_search(
            session=session,
            tool_name="search_oneshot",
            arguments={
                "query": query,
                "earliest_time": "0",
                "latest_time": "now",
                "max_count": 50,
                "output_format": "json",
            },
            case_state=case_state,
            metrics=metrics,
            round_number=0,
            active_skills=active_skills,
            phase=phase,
            question=question,
        )
        outputs.append((label, model_result))
        if evidence:
            evidence_ids.append(evidence["evidence_id"])
        if case_state.get("fatal_tool_error"):
            # Authentication/permission/index failures cannot be repaired by an LLM.
            # Stop the bootstrap immediately instead of running the CHECK query.
            break

    # ALR legacy compatibility deliberately does not hard-code semantic result interpretation here.
    # The result_semantics_recovery skill reviews the evidence after bootstrap and
    # decides whether to accept, reinterpret, query again, reject, or stop.
    return evidence_ids, outputs, None
