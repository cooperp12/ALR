import re


def infer_investigation_type(question, active_skills):
    """Infer the *primary* investigation family from the question first.

    Loaded recovery skills must not silently reclassify the primary problem. For
    example, temporal_pattern_analysis may be available to an aggregation case
    without making that case a temporal question.
    """
    low = (question or "").lower()

    if any(x in low for x in ("before and after", "before/after", "timeline", "blast radius", "sequence of events")):
        return "timeline"
    if any(x in low for x in ("distinct", "unique", "different errors", "most errors")):
        return "aggregation"
    if any(x in low for x in ("first attempt", "earliest", "initial attempt", "first event")):
        return "chronological"
    if any(x in low for x in ("over time", "trend", "spike", "burst", "time series", "change point", "periodic")):
        return "temporal"

    # Skill-based fallback only when the wording itself did not identify a family.
    if "bidirectional_timeframe" in active_skills:
        return "timeline"
    if "spl_distinct_count" in active_skills:
        return "aggregation"
    if "chronological_first_event" in active_skills:
        return "chronological"
    if "temporal_pattern_analysis" in active_skills:
        return "temporal"
    if "external_enrichment" in active_skills:
        return "external_enrichment"
    return "direct_lookup"


def active_query_contracts(active_skills, phase="DO"):
    contracts = []

    if "spl_distinct_count" in active_skills and phase == "DO":
        contracts.append(
            {
                "skill": "spl_distinct_count",
                "kind": "hard",
                "description": "Primary distinct-count work must aggregate with stats dc(...).",
                "required_regex": r"\|\s*stats\b[^|]*\bdc\s*\(",
            }
        )

    if "aws_cloudtrail" in active_skills and phase == "DO":
        contracts.append(
            {
                "skill": "aws_cloudtrail",
                "kind": "hard",
                "description": "AWS primary searches must target aws:cloudtrail rather than raw all-source sampling.",
                "required_regex": r"\bsourcetype\s*=\s*[\"']?aws:cloudtrail[\"']?",
            }
        )

    return contracts


def enforce_query_contracts(query, active_skills, phase="DO"):
    if not query:
        return None

    violations = []
    for contract in active_query_contracts(active_skills, phase=phase):
        pattern = contract.get("required_regex")
        if pattern and not re.search(pattern, query, flags=re.IGNORECASE):
            violations.append(contract)

    if not violations:
        return None

    return {
        "guardrail": "Active skill query contract blocked this search.",
        "phase": phase,
        "violations": [
            {
                "skill": item["skill"],
                "requirement": item["description"],
            }
            for item in violations
        ],
        "required_action": (
            "Rewrite the SPL to follow the loaded skills. Do not use a broad raw "
            "search when a selected skill requires a reducing aggregation."
        ),
    }


def query_method_class(query):
    low = (query or "").lower()
    if "dc(" in low and "| stats" in low:
        return "aggregation_distinct"
    if "values(" in low and "| stats" in low:
        return "aggregation_values"
    if "eventstats" in low:
        return "eventstats"
    if "| stats" in low:
        return "aggregation_other"
    if "| sort" in low and "| head 1" in low:
        return "chronological_first"
    if "| table" in low or "| fields" in low:
        return "targeted_rows"
    return "raw_search"


def semantic_query_shape(query):
    # Terminal ordering alone does not change the evidence. Head/tail and sorting
    # before a limit do change it and must never be discarded by cache comparison.
    from commander_agent.state.scope import split_spl_pipeline
    stages = split_spl_pipeline(query)
    while stages and re.match(r"^sort\b", stages[-1], re.I):
        stages.pop()
    return " | ".join(re.sub(r"\s+", " ", part.strip()) for part in stages)
