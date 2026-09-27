"""Universal semantic/query relationship guard for ALR legacy compatibility.

Known semantic relationships are deterministic invariants. The Supervisor may decide
whether a relationship should be reopened, but it cannot make a violating SPL query
safe to execute. This module runs on every Splunk query path, including free-form,
bootstrap and recovery searches.
"""
from __future__ import annotations

import re

from commander_agent.semantic.binding import semantic_binding_query_violations
from commander_agent.state.evidence_store import (
    active_measurement_contract,
    measurement_contract_violations,
)
from commander_agent.state.semantic_state import active_bindings
from commander_agent.state.extraction import active_extraction_contract


def _violation(code, message, *, relationship="", expected="", actual="", remedy=""):
    return {
        "code": code,
        "message": message,
        "relationship": relationship,
        "expected": expected,
        "actual": actual,
        "remedy": remedy,
    }


def _multivalue_len_violations(query: str):
    """Detect values(field)->len(alias), which measures string length, not cardinality."""
    q = str(query or "")
    aliases = {}
    for field, alias in re.findall(
        r"\bvalues\s*\(\s*([^\)]+?)\s*\)\s+(?:AS\s+)?([A-Za-z_][\w.]*)",
        q,
        flags=re.I,
    ):
        aliases[str(alias).lower()] = str(field).strip()

    out = []
    for target, source_field in aliases.items():
        if re.search(r"\blen\s*\(\s*" + re.escape(target) + r"\s*\)", q, flags=re.I):
            out.append(
                _violation(
                    "MULTIVALUE_LEN_CARDINALITY_ERROR",
                    f"len({target}) measures text length, not the number of values produced by values({source_field}).",
                    relationship="multivalue_cardinality",
                    expected=f"mvcount({target}) or dc({source_field})",
                    actual=f"len({target})",
                    remedy="Use mvcount() for a values(...) multivalue field, or use dc(field) directly.",
                )
            )
    return out


def _population_completeness_violations(query: str, case_state):
    """Ranking evidence must preserve the complete grouped population before Python ranks it."""
    frame = case_state.get("investigation_frame") or {}
    if frame.get("aggregation") != "distinct_count" or frame.get("ranking") not in {"maximum", "minimum"}:
        return []
    if active_measurement_contract(case_state):
        # After the full measurement has been contracted, targeted validation queries may
        # legitimately narrow to a candidate. Contract drift is guarded separately.
        return []
    q = str(query or "")
    if not re.search(r"\|\s*stats\b[^|]*\bBY\b", q, flags=re.I):
        return []
    if not re.search(r"\|\s*head\s+1\b", q, flags=re.I):
        return []
    return [
        _violation(
            "INCOMPLETE_RANKING_POPULATION",
            "A distinct-ranking query used head 1 before Python had a complete grouped population.",
            relationship="ranking_population_completeness",
            expected="all grouped rows for deterministic ranking",
            actual="head 1 before measurement contract",
            remedy="Return the full grouped population (within transport limits), create the measurement contract, then let Python derive the maximum/minimum.",
        )
    ]



def _source_contract_violations(query: str, case_state):
    contract = case_state.get("source_contract") or {}
    source = contract.get("source")
    q = str(query or "")
    if not q:
        return []
    out = []
    if source == "email" and re.search(r"\bsourcetype\s*=\s*[\"']?aws:cloudtrail", q, re.I):
        out.append(_violation(
            "SOURCE_CONTRACT_MISMATCH",
            "The active source contract requires message/email evidence, but the proposed query targets CloudTrail.",
            relationship="source_contract_to_query", expected="stream:smtp or O365 message evidence", actual=q,
            remedy="Use the email_investigation source route or explicitly reopen source resolution.",
        ))
    if source == "aws_cloudtrail" and "sourcetype=aws:cloudtrail" not in q.lower().replace('"','').replace("'",''):
        # Only enforce when the query is clearly a Splunk source query.
        if "index=" in q.lower() and "sourcetype=" in q.lower():
            out.append(_violation(
                "SOURCE_CONTRACT_MISMATCH",
                "The active source contract requires AWS CloudTrail evidence.",
                relationship="source_contract_to_query", expected="sourcetype=aws:cloudtrail", actual=q,
                remedy="Query CloudTrail or explicitly reopen source resolution.",
            ))
    return out


def _extraction_contract_violations(query: str, case_state):
    contract = active_extraction_contract(case_state)
    if not contract:
        return []
    q = str(query or "")
    out = []
    output_field = str(contract.get("output_field") or "").strip()
    if output_field and contract.get("source") == "aws_cloudtrail":
        projection = re.search(r"\|\s*(?:fields|table)\s+([^|]+)", q, re.I)
        if projection and output_field.lower() not in projection.group(1).lower():
            out.append(_violation(
                "EXTRACTION_OUTPUT_FIELD_MISSING",
                f"The active extraction contract requires preserving {output_field} in returned evidence.",
                relationship="extraction_contract_to_query", expected=output_field, actual=projection.group(1).strip(),
                remedy=f"Include {output_field} in the projection or explicitly reopen the extraction contract.",
            ))
    for field, value in (contract.get("entity_filters") or {}).items():
        if field and value and contract.get("source") == "aws_cloudtrail":
            if str(value).lower() not in q.lower():
                out.append(_violation(
                    "EXTRACTION_ENTITY_FILTER_MISSING",
                    f"The active extraction contract requires the known entity filter {field}={value}.",
                    relationship="extraction_contract_to_query", expected=f"{field}={value}", actual=q,
                    remedy="Preserve the strong known entity while discovering the unknown event semantics.",
                ))
    if contract.get("discovery_before_specificity") and contract.get("source") == "aws_cloudtrail":
        m = re.search(r"\beventName\s*=\s*([A-Za-z0-9_*?-]+)", q, re.I)
        if m:
            proposed = m.group(1).strip().strip('"\'')
            observed = {str(x).lower() for x in (case_state.get("discovery_state", {}).get("observed_event_names") or [])}
            if proposed and "*" not in proposed and proposed.lower() not in observed:
                out.append(_violation(
                    "UNOBSERVED_EVENT_SPECIFICITY",
                    f"Exact eventName={proposed} was introduced before that operation was observed in evidence.",
                    relationship="discovery_before_specificity", expected="broad entity discovery first", actual=f"eventName={proposed}",
                    remedy="Query the known entity broadly, observe eventName values, then narrow to an observed operation.",
                ))
    return out

def semantic_query_policy_violations(query, case_state, phase="DO"):
    """Return deterministic semantic/query violations for any Splunk execution path."""
    violations = []
    violations.extend(_source_contract_violations(query, case_state))
    violations.extend(_extraction_contract_violations(query, case_state))
    bindings = active_bindings(case_state)
    for message in semantic_binding_query_violations(query, bindings):
        violations.append(
            _violation(
                "SEMANTIC_BINDING_QUERY_MISMATCH",
                message,
                relationship="semantic_binding_to_query",
                expected="query must implement the active semantic binding",
                actual=str(query or ""),
                remedy="Repair the SPL to match the active binding, or explicitly reopen/invalidate that binding through supervised semantic recovery.",
            )
        )

    contract = active_measurement_contract(case_state)
    for message in measurement_contract_violations(query, contract):
        violations.append(
            _violation(
                "MEASUREMENT_CONTRACT_VIOLATION",
                message,
                relationship="measurement_contract_to_query",
                expected="query must preserve the active measurement contract",
                actual=str(query or ""),
                remedy="Use the contracted group/measurement fields or explicitly reopen measurement semantics.",
            )
        )

    violations.extend(_multivalue_len_violations(query))
    violations.extend(_population_completeness_violations(query, case_state))

    # Stable de-duplication by code/message keeps supervisor prompts compact.
    seen = set()
    out = []
    for item in violations:
        key = (item.get("code"), item.get("message"))
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out
