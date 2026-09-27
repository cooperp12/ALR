from __future__ import annotations

import json
import re
from pathlib import Path

from commander_agent.config import (
    ARTIFACT_DIR,
    BOTSV3_EARLIEST,
    BOTSV3_LATEST,
    MAX_SEARCH_EXECUTIONS,
    TEMPORAL_MAX_REDUCED_ROWS,
)
from commander_agent.mcp.adapter import tool_result_to_text
from commander_agent.mcp.guardrails import apply_search_guardrail
from commander_agent.mcp.logging import append_full_result_log
from commander_agent.skills.contracts import enforce_query_contracts
from commander_agent.skills.structured_output_validation.scripts.schema import (
    validate_search_arguments,
    validate_search_result_text,
)
from commander_agent.state.evidence import evidence_model_text, record_evidence
from commander_agent.state.io import stable_hash
from commander_agent.state.scope import scope_violations


_ALLOWED_MEASURES = {
    "event_count",
    "distinct_count",
    "cumulative_distinct_count",
    "new_distinct_count",
}
_FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]*$")


def _safe_field(value, allow_none=False):
    if value in (None, "") and allow_none:
        return None
    text = str(value or "").strip()
    if not _FIELD_RE.fullmatch(text):
        raise ValueError(f"Unsafe/invalid SPL field name: {text!r}")
    return text


def normalise_analysis_request(request: dict) -> dict:
    if not isinstance(request, dict):
        raise ValueError("analysis_request must be an object")
    group_field = _safe_field(request.get("group_field"), allow_none=True)
    value_field = _safe_field(request.get("value_field"), allow_none=True)
    time_field = _safe_field(request.get("time_field"))
    measures = []
    for item in request.get("measures") or []:
        name = str(item)
        if name in _ALLOWED_MEASURES and name not in measures:
            measures.append(name)
    if not measures:
        raise ValueError("analysis_request.measures must contain at least one supported measure")
    if any(x in measures for x in ("distinct_count", "cumulative_distinct_count", "new_distinct_count")) and not value_field:
        raise ValueError("distinct-value temporal measures require value_field")
    comparison = request.get("comparison_feature")
    comparison = str(comparison).strip() if comparison not in (None, "") else None
    return {
        "group_field": group_field,
        "value_field": value_field,
        "time_field": time_field,
        "measures": measures,
        "comparison_feature": comparison,
    }


def build_reduction_spl(scope_contract: dict, request: dict) -> str:
    base = str((scope_contract or {}).get("base_scope_spl") or "").strip()
    if not base:
        raise ValueError("Temporal analysis requires an established scope contract")
    req = normalise_analysis_request(request)
    time_field = req["time_field"]
    group_field = req["group_field"]
    value_field = req["value_field"]
    measures = set(req["measures"])

    aggs = []
    if "event_count" in measures:
        aggs.append("count AS event_count")
    else:
        # Always retain volume as context for the reduced time series. This is a
        # deterministic supporting measurement, not a threshold or answer rule.
        aggs.append("count AS event_count")
    if "distinct_count" in measures or "cumulative_distinct_count" in measures or "new_distinct_count" in measures:
        aggs.append(f"dc({value_field}) AS distinct_count")
    if "cumulative_distinct_count" in measures or "new_distinct_count" in measures:
        aggs.append(f"values({value_field}) AS distinct_values")

    by = [time_field]
    if group_field:
        by.append(group_field)
    # `bin <time-field>` deliberately leaves span unspecified. Splunk selects the
    # binning from the scoped data/time range; ALR legacy compatibility does not encode a fixed window.
    return (
        f"{base} | bin {time_field} | stats {' '.join(aggs)} BY {' '.join(by)} "
        f"| sort 0 {time_field}" + (f" {group_field}" if group_field else "")
    )


async def run_temporal_analysis(session, question, case_state, metrics, active_skills, request):
    req = normalise_analysis_request(request)
    query = build_reduction_spl(case_state.get("scope_contract") or {}, req)
    violations = scope_violations(query, case_state.get("scope_contract"))
    if violations:
        return {
            "ok": False,
            "reason": "Generated temporal reduction violated the current scope contract.",
            "violations": violations,
        }, None

    arguments = {
        "query": query,
        "earliest_time": BOTSV3_EARLIEST,
        "latest_time": BOTSV3_LATEST,
        "max_count": TEMPORAL_MAX_REDUCED_ROWS,
        "output_format": "json",
    }
    metrics["search_attempts"] = metrics.get("search_attempts", 0) + 1
    guarded_args, blocked = apply_search_guardrail("search_export", arguments)
    if blocked:
        metrics["guardrail_blocks"] = metrics.get("guardrail_blocks", 0) + 1
        return {"ok": False, "reason": "Search guardrail blocked temporal reduction.", "details": blocked}, None

    args_ok, canonical_args, errors = validate_search_arguments(guarded_args)
    if not args_ok:
        metrics["structured_output_rejections"] = metrics.get("structured_output_rejections", 0) + 1
        return {"ok": False, "reason": "Temporal query object failed validation.", "errors": errors}, None
    query = canonical_args["query"]

    contract_block = enforce_query_contracts(query, active_skills, phase="CHECK")
    if contract_block:
        metrics["skill_contract_blocks"] = metrics.get("skill_contract_blocks", 0) + 1
        return {"ok": False, "reason": "Active skill contract blocked temporal reduction.", "details": contract_block}, None

    if metrics.get("search_calls", 0) >= MAX_SEARCH_EXECUTIONS:
        return {"ok": False, "reason": "Search execution budget reached before temporal analysis."}, None

    metrics["search_calls"] = metrics.get("search_calls", 0) + 1
    metrics["temporal_analysis_calls"] = metrics.get("temporal_analysis_calls", 0) + 1
    metrics.setdefault("queries", []).append(query)

    try:
        result = await session.call_tool("search_export", arguments=canonical_args)
        result_text = tool_result_to_text(result)
    except Exception as exc:
        metrics["tool_errors"] = metrics.get("tool_errors", 0) + 1
        return {"ok": False, "reason": f"Temporal Splunk reduction failed: {type(exc).__name__}: {exc}"}, None

    append_full_result_log(0, "search_export", canonical_args, result_text)
    validation = validate_search_result_text(result_text)
    if not validation.get("ok"):
        metrics["tool_errors"] = metrics.get("tool_errors", 0) + 1
        failure = validation.get("failure") or {}
        if failure.get("fatal"):
            case_state["fatal_tool_error"] = {
                "kind": failure.get("kind", validation.get("kind", "tool_error")),
                "code": failure.get("code", "SPLUNK_FATAL_ERROR"),
                "message": failure.get("message") or "; ".join(validation.get("errors", [])),
                "layer": failure.get("layer", "splunk"),
                "retryable": bool(failure.get("retryable", False)),
                "round": 0,
            }
            metrics["fatal_tool_errors"] = metrics.get("fatal_tool_errors", 0) + 1
        return {
            "ok": False,
            "reason": "Temporal reduced-result validation failed.",
            "kind": validation.get("kind"),
            "errors": validation.get("errors", []),
        }, None

    payload = validation["payload"]
    rows = payload.get("events", payload.get("results")) or []
    if not rows:
        metrics["zero_results"] = metrics.get("zero_results", 0) + 1
        return {"ok": False, "reason": "Temporal reduction returned no rows."}, None

    from commander_agent.skills.temporal_pattern_analysis.scripts.analytics import analyse_rows, write_graphs
    analysis = analyse_rows(rows, req)
    if not analysis.get("ok"):
        return analysis, None

    case_id = str(case_state.get("case_id") or "case")
    signature = stable_hash({"query": query, "request": req})[:16]
    out_dir = Path(ARTIFACT_DIR) / case_id
    graph_paths = write_graphs(analysis, out_dir, f"temporal_{signature}")
    analysis["query"] = query
    analysis["graph_paths"] = graph_paths
    # The feature JSON is the machine evidence. Chart rows are useful for graphing but
    # unnecessarily verbose for the LLM/evidence ledger, so remove them from the record.
    analysis_for_evidence = dict(analysis)
    analysis_for_evidence.pop("chart_rows", None)

    output_dir = Path(ARTIFACT_DIR) / case_id
    output_dir.mkdir(parents=True, exist_ok=True)
    feature_path = output_dir / f"temporal_{signature}_features.json"
    feature_path.write_text(json.dumps(analysis_for_evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    analysis_for_evidence["feature_path"] = str(feature_path)

    model_result = json.dumps(analysis_for_evidence, ensure_ascii=False, indent=2)
    evidence = record_evidence(
        case_state,
        source="analytics",
        tool_name="temporal_pattern_analysis",
        model_result=model_result,
        query=query,
        signature=stable_hash({"temporal_analysis": query, "request": req}),
        cache_label="fresh deterministic analytics",
        method_class="temporal_pattern_analysis",
    )
    metrics["temporal_analysis_rows"] = metrics.get("temporal_analysis_rows", 0) + len(rows)
    metrics["temporal_analysis_groups"] = metrics.get("temporal_analysis_groups", 0) + int(analysis.get("group_count", 0))
    metrics["temporal_graphs"] = metrics.get("temporal_graphs", 0) + len(graph_paths)
    return analysis_for_evidence, evidence
