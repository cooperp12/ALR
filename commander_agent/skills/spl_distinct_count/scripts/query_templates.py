"""Executable query templates for distinct-count work.

ALR legacy compatibility separates concept interpretation from physical field binding. Query templates
consume the Investigation Frame + semantic bindings when available; they never use
benchmark answers.
"""
from __future__ import annotations

import re

from commander_agent.semantic.binding import binding_for


def _nonempty(field):
    return f'| where isnotnull({field}) AND len(trim({field}))>0'


def _failure_filter(field="errorCode"):
    return (
        f'| where isnotnull({field}) AND len(trim({field}))>0 '
        f'AND NOT match(lower({field}), "^(success|ok|none|null|n/a|na|passed|-)$")'
    )


def cloudtrail_distinct_plan(question, investigation_frame=None, semantic_bindings=None):
    low = (question or "").lower()
    frame = investigation_frame or {}
    bindings = semantic_bindings or []

    measure_binding = binding_for(bindings, kind="measurement_field")
    group_binding = binding_for(bindings, kind="group_field")
    distinct_field = (measure_binding or {}).get("field")
    group_field = (group_binding or {}).get("field")

    # Backwards-compatible fallbacks are concept heuristics, not benchmark answers.
    if not distinct_field:
        role = frame.get("measure_role")
        if role == "detailed_failure_description":
            distinct_field = "errorMessage"
        elif role == "failure_category":
            distinct_field = "errorCode"
        else:
            distinct_field = "errorCode" if "error" in low else "eventName"
    if not group_field:
        if "access key" in low or "accesskey" in low:
            group_field = "userIdentity.accessKeyId"
        elif "username" in low or "user name" in low or "iam user" in low:
            group_field = "userIdentity.userName"
        else:
            group_field = "userIdentity.accessKeyId"

    filters = ["index=botsv3", "sourcetype=aws:cloudtrail"]
    for b in bindings:
        if b.get("status") != "SUPPORTED":
            continue
        if b.get("kind") == "filter" and b.get("field") and b.get("value") not in (None, ""):
            filters.append(f"{b['field']}={b['value']}")
        if b.get("kind") == "presence_filter" and b.get("field"):
            filters.append(f"{b['field']}=*")
    if "iam" in low and not any(x.startswith("eventSource=") for x in filters):
        filters.append("eventSource=iam.amazonaws.com")
    if frame.get("measure_concept") == "error" and not any(x.startswith("errorCode=") for x in filters):
        filters.append("errorCode=*")

    base = " ".join(dict.fromkeys(filters))
    clauses = []
    # Failure population and measured representation are separate concerns.
    if frame.get("measure_concept") == "error" or "error" in low:
        clauses.append(_failure_filter("errorCode"))
    if distinct_field != "errorCode":
        clauses.append(_nonempty(distinct_field))
    where = " " + " ".join(clauses) if clauses else ""

    primary = (
        f"{base}{where} | stats dc({distinct_field}) AS distinct_count "
        f"count AS matching_events BY {group_field} "
        f"| eventstats count AS population_keys "
        f"| sort 0 {group_field}"
    )
    check = (
        f"{base}{where} | stats values({distinct_field}) AS distinct_values "
        f"count AS matching_events BY {group_field} | sort 0 {group_field}"
    )
    return {
        "primary_query": re.sub(r"\s+", " ", primary).strip(),
        "check_query": re.sub(r"\s+", " ", check).strip(),
        "group_field": group_field,
        "distinct_field": distinct_field,
        "semantic_review_skill": "result_semantics_recovery",
        "semantic_binding_id": (measure_binding or {}).get("binding_id"),
    }
