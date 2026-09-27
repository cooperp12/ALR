from commander_agent.skills.contracts import infer_investigation_type
from commander_agent.skills.spl_distinct_count.scripts.query_templates import cloudtrail_distinct_plan
from commander_agent.semantic.contracts import build_source_contract, build_extraction_contract, direct_bootstrap_strategy


def _contains(question, *terms):
    low = (question or "").lower()
    return any(term in low for term in terms)


def compose_query_strategy(question, active_skills, investigation_frame=None, semantic_bindings=None):
    """
    Compose reusable skill capabilities. This is semantic, not Q-number mapping and
    contains no expected answers.

    High-confidence compositions may bootstrap small reducing queries before the
    LLM agent loop. This prevents a small model from ignoring a selected skill and
    dumping broad raw events into context.
    """
    kind = infer_investigation_type(question, active_skills)
    strategy = {
        "confidence": "medium",
        "investigation_type": kind,
        "bootstrap": False,
        "primary_query": "",
        "check_query": "",
        "guidance": "",
        "method_primary": "",
        "method_check": "",
        "resolver": "",
        "resolver_config": {},
        "map_first": kind == "timeline",
        "investigation_frame": investigation_frame or {},
        "semantic_bindings": list(semantic_bindings or []),
    }

    frame = investigation_frame or {}
    source_contract = build_source_contract(frame, question)
    extraction_contract = build_extraction_contract(frame, question, semantic_bindings, source_contract)
    strategy["source_contract"] = source_contract
    strategy["extraction_contract"] = extraction_contract

    # ALR legacy compatibility direct-extraction contracts take precedence over weak model routing.
    # This prevents support-case IDs being treated as CloudTrail error fields and
    # prevents user-agent questions from guessing eventName before discovery.
    direct = direct_bootstrap_strategy(frame, question, source_contract, extraction_contract)
    if direct:
        strategy.update({
            "confidence": "high",
            "investigation_type": "direct_lookup",
            "bootstrap": True,
            "primary_query": direct.get("primary_query", ""),
            "check_query": direct.get("check_query", ""),
            "method_primary": direct.get("method_primary", "targeted_rows"),
            "method_check": direct.get("method_check", "targeted_rows"),
            "resolver": "direct_extraction",
            "dynamic_check": bool(direct.get("dynamic_check")),
            "guidance": (
                "Use the active Source Contract and Extraction Contract. Discover observed events before "
                "introducing an exact eventName that is not yet in evidence. Preserve the contracted output "
                "field and let Python extract/verify the candidate from machine evidence."
            ),
        })
        return strategy
    if frame.get("requires_reference_follow"):
        strategy.update({
            "confidence": "high",
            "investigation_type": "multi_step",
            "guidance": (
                "Use the Investigation Frame primitives in order: select the source artifact, "
                "extract the reference, follow that reference, then extract and independently "
                "validate the requested value. Do not keep reformulating one-source SPL after "
                "the frame says REFERENCE_FOLLOW is required."
            ),
        })
        return strategy
    if frame.get("requires_external_enrichment"):
        strategy.update({
            "confidence": "high",
            "investigation_type": "external_enrichment",
            "guidance": (
                "First obtain the requested external identifier from the scoped event evidence. "
                "Apply any temporal constraint from the Investigation Frame before selecting it, "
                "then use external enrichment to map that identifier to the requested knowledge."
            ),
        })
        return strategy

    # Generic composition: CloudTrail + distinct-count. Determine the grouping
    # concept and distinct concept from the user's wording.
    if "spl_distinct_count" in active_skills and "aws_cloudtrail" in active_skills:
        templates = cloudtrail_distinct_plan(question, investigation_frame=investigation_frame, semantic_bindings=semantic_bindings)
        distinct_field = templates["distinct_field"]
        group_field = templates["group_field"]

        strategy.update(
            {
                "confidence": "high",
                "bootstrap": True,
                "primary_query": templates["primary_query"],
                "check_query": templates["check_query"],
                "method_primary": "aggregation_distinct",
                "method_check": "aggregation_values",
                "resolver": "result_semantics_recovery",
                "resolver_config": {
                    "group_field": templates.get("group_field"),
                    "distinct_field": templates.get("distinct_field"),
                },
                "semantic_review_skill": templates.get("semantic_review_skill", "semantic_layer"),
                "guidance": (
                    "This is an aggregation task. The Investigation Frame and semantic layer "
                    f"bind the requested concept to dc({distinct_field}) BY {group_field} before "
                    "query execution. Keep failure-population filters separate from the measured "
                    "field. If later evidence contradicts the binding's granularity, invalidate/rebind "
                    "the semantic field rather than inventing a tie-break."
                ),
            }
        )
        return strategy

    if "temporal_pattern_analysis" in active_skills:
        strategy["guidance"] = (
            "This investigation benefits from temporal behaviour. First establish a tightly scoped "
            "event population with an efficient Splunk query. Then use run_temporal_analysis so "
            "Splunk performs server-side time-bucket reduction and Polars/NumPy compute trends, "
            "changes, diversity growth, and comparisons. Do not estimate numeric trends from raw rows."
        )
        return strategy

    if "chronological_first_event" in active_skills:
        strategy["guidance"] = (
            "This is chronological. Filter to the candidate event set, sort ascending "
            "by _time/eventTime, then head 1. Do not infer 'first' from default order."
        )
        return strategy

    if "bidirectional_timeframe" in active_skills:
        strategy["map_first"] = True
        strategy["guidance"] = (
            "Use the executable case map first. Establish an evidence-backed T0 with "
            "set_timeline_anchor, inspect timeline_before/timeline_after at +/-30m, "
            "pivot with timeline_related, and widen to +/-2h then +/-24h only when "
            "coverage remains incomplete."
        )
        return strategy

    if "external_enrichment" in active_skills:
        strategy["guidance"] = (
            "Discover the exact external artefact in Splunk first. Only then enrich that "
            "specific URL/AMI/identifier from a public source."
        )
        return strategy

    strategy["guidance"] = (
        "Use the cheapest targeted query that directly establishes the requested fact, "
        "then cross-check with a materially different evidence path."
    )
    return strategy


def strategy_prompt_text(strategy):
    lines = [
        f"Investigation type: {strategy.get('investigation_type')}",
        f"Strategy confidence: {strategy.get('confidence')}",
        strategy.get("guidance", ""),
    ]
    if strategy.get("primary_query"):
        lines.append("Preferred primary SPL:\n" + strategy["primary_query"])
    if strategy.get("check_query"):
        lines.append("Preferred independent check SPL:\n" + strategy["check_query"])
    return "\n".join(x for x in lines if x)
