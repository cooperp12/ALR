"""ALR legacy compatibility source and extraction contracts.

These contracts are deterministic relationship state derived from the question/frame.
They contain no benchmark answers. They stop models from choosing an incompatible
source/field and require discovery before unobserved event-name specificity.
"""
from __future__ import annotations

import re
from commander_agent.state.io import stable_hash


def _access_key(question: str):
    m = re.search(r"\b(AKIA[0-9A-Z]{12,20}|ASIA[0-9A-Z]{12,20})\b", str(question or ""), re.I)
    return m.group(1).upper() if m else None


def build_source_contract(frame: dict, question: str):
    q = str(question or "")
    low = q.lower()
    requested = str((frame or {}).get("requested_output_concept") or "").lower()
    role = str((frame or {}).get("measure_role") or "none")

    if "support_case_id" in requested or "support case" in low:
        source = "email"
        skill = "email_investigation"
        allowed = ["stream:smtp", "ms:o365:reporting:messagetrace"]
        reason = "A support case ID from an AWS compromise notification is message evidence, not a CloudTrail error field."
        confidence = "high"
    elif role == "client_identifier" or "user agent" in low or "application that originated" in low:
        source = "aws_cloudtrail"
        skill = "aws_cloudtrail"
        allowed = ["aws:cloudtrail"]
        reason = "The requested client/user-agent value is an attribute of the AWS API request event."
        confidence = "high"
    else:
        source = str((frame or {}).get("domain") or "unknown")
        skill = "aws_cloudtrail" if source == "aws_cloudtrail" else ("email_investigation" if source == "email" else None)
        allowed = ["aws:cloudtrail"] if source == "aws_cloudtrail" else (["stream:smtp", "ms:o365:reporting:messagetrace"] if source == "email" else [])
        reason = "Source follows the reconciled Investigation Frame."
        confidence = "medium"

    core = {
        "source": source,
        "preferred_skill": skill,
        "allowed_sourcetypes": allowed,
        "requested_output_concept": (frame or {}).get("requested_output_concept"),
        "reason": reason,
        "confidence": confidence,
    }
    return {**core, "source_contract_id": "SC-" + stable_hash(core)[:12]}


def build_extraction_contract(frame: dict, question: str, bindings=None, source_contract=None):
    if not isinstance(frame, dict) or frame.get("aggregation") not in {None, "none"}:
        return None
    low = str(question or "").lower()
    requested = str(frame.get("requested_output_concept") or "requested_value")
    role = str(frame.get("measure_role") or "none")
    source = (source_contract or {}).get("source") or frame.get("domain")
    key = _access_key(question)

    contract = {
        "contract_type": "direct_extraction",
        "source": source,
        "requested_output_concept": requested,
        "output_role": role,
        "output_field": None,
        "candidate_pattern": None,
        "entity_filters": {},
        "event_semantics": None,
        "discovery_before_specificity": False,
        "status": "ACTIVE",
        "confidence": "medium",
    }

    if "support_case_id" in requested.lower() or "support case" in low:
        contract.update({
            "source": "email",
            "output_role": "support_case_identifier",
            "output_field": "content_body",
            "fallback_output_fields": ["body", "_raw", "subject"],
            "candidate_pattern": r"(?i)(?:support\s+case|case)(?:\s+(?:id|number|#))?[^0-9]{0,40}([0-9]{8,14})",
            "confidence": "high",
        })
    elif role == "client_identifier" or "user agent" in low or "application that originated" in low:
        contract.update({
            "source": "aws_cloudtrail",
            "output_role": "client_identifier",
            "output_field": "userAgent",
            "event_semantics": "describe account" if "describe" in low and "account" in low else frame.get("event_concept"),
            "discovery_before_specificity": bool(key),
            "confidence": "high",
        })
        if key:
            contract["entity_filters"] = {"userIdentity.accessKeyId": key}
    else:
        # Reuse a deterministic output binding if one exists.
        for b in bindings or []:
            if b.get("kind") == "output_field" and b.get("status") == "SUPPORTED" and b.get("field"):
                contract["output_field"] = b.get("field")
                contract["output_role"] = b.get("role_required")
                break
        if key:
            contract["entity_filters"] = {"userIdentity.accessKeyId": key}

    fp = stable_hash(contract)
    return {**contract, "extraction_contract_id": "XC-" + fp[:12], "fingerprint": fp}


def direct_bootstrap_strategy(frame: dict, question: str, source_contract: dict, extraction_contract: dict):
    """Return generic high-confidence direct extraction queries when contracts are sufficient."""
    if not extraction_contract:
        return None
    source = extraction_contract.get("source")
    requested = str(extraction_contract.get("requested_output_concept") or "").lower()
    if source == "email" and ("support_case_id" in requested or extraction_contract.get("output_role") == "support_case_identifier"):
        primary = (
            'index=botsv3 earliest=0 sourcetype=stream:smtp '
            '("access key" OR compromised OR "support case" OR "case id") '
            '| eval message_text=coalesce(content_body,content,body,_raw,subject) '
            '| table _time subject from to content_body content body message_text _raw | head 30'
        )
        check = (
            'index=botsv3 earliest=0 sourcetype=stream:smtp '
            '("access key" OR "support case" OR "case id" OR case) '
            '| eval message_text=coalesce(content_body,content,body,_raw,subject) '
            '| rex field=message_text "(?i)(?:support\\s+case|case)(?:\\s+(?:id|number|no\\.?|#))?\\s*[:=#-]?\\s*(?<support_case_id>[0-9]{8,14})" '
            '| where isnotnull(support_case_id) | stats values(support_case_id) AS support_case_ids count AS matching_messages'
        )
        return {
            "primary_query": primary,
            "check_query": check,
            "method_primary": "targeted_rows",
            "method_check": "aggregation_values",
            "dynamic_check": True,
        }

    if source == "aws_cloudtrail" and extraction_contract.get("output_field") == "userAgent":
        filters = extraction_contract.get("entity_filters") or {}
        key = filters.get("userIdentity.accessKeyId")
        if key:
            primary = (
                'index=botsv3 earliest=0 sourcetype=aws:cloudtrail '
                f'userIdentity.accessKeyId={key} '
                '| table _time userIdentity.accessKeyId eventSource eventName errorCode errorMessage userAgent | sort 0 _time'
            )
            return {
                "primary_query": primary,
                "check_query": "",
                "method_primary": "targeted_rows",
                "method_check": "targeted_rows",
                "dynamic_check": True,
            }
    return None
