"""Small dependency-free JSON/schema validation helpers for ALR legacy compatibility.

This deliberately avoids `default=str` when validating objects. If an object cannot
round-trip as real JSON, ALR legacy compatibility does not persist it as trusted structured state.
"""

from __future__ import annotations

import json
import re
from typing import Any

from commander_agent.mcp.failures import classify_splunk_failure


SUCCESS_SENTINELS = {"success", "ok", "none", "null", "n/a", "na", "-", "passed"}


def strict_json_roundtrip(value: Any):
    """Return (ok, canonical_value, error)."""
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        loaded = json.loads(text)
        return True, loaded, ""
    except Exception as exc:
        return False, None, f"{type(exc).__name__}: {exc}"


def _extract_json_candidate(text: str):
    value = (text or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
        value = re.sub(r"\s*```$", "", value)
    return value.strip()


def parse_json_text(text: str, expected_type=None, allow_embedded=False):
    """Parse JSON and optionally require a Python container type.

    `allow_embedded` is intentionally off by default. Model contracts can opt into
    extracting the outermost object, but persistent/tool data uses strict JSON.
    """
    value = _extract_json_candidate(text)
    try:
        parsed = json.loads(value)
    except Exception as first_exc:
        if not allow_embedded:
            return False, None, f"invalid JSON: {first_exc}"
        start_obj, end_obj = value.find("{"), value.rfind("}")
        start_arr, end_arr = value.find("["), value.rfind("]")
        candidates = []
        if start_obj >= 0 and end_obj > start_obj:
            candidates.append(value[start_obj:end_obj + 1])
        if start_arr >= 0 and end_arr > start_arr:
            candidates.append(value[start_arr:end_arr + 1])
        parsed = None
        last = first_exc
        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
                break
            except Exception as exc:
                last = exc
        if parsed is None:
            return False, None, f"invalid JSON: {last}"

    if expected_type is not None and not isinstance(parsed, expected_type):
        return False, None, f"expected {expected_type.__name__}, got {type(parsed).__name__}"
    ok, canonical, error = strict_json_roundtrip(parsed)
    return ok, canonical, error


def _check_type(value, type_name):
    if type_name == "object":
        return isinstance(value, dict)
    if type_name == "array":
        return isinstance(value, list)
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "null":
        return value is None
    return True


def validate_shape(value, schema: dict, path="$", errors=None):
    """Validate a useful subset of JSON Schema without extra dependencies."""
    errors = errors if errors is not None else []
    expected = schema.get("type")
    if expected:
        allowed = expected if isinstance(expected, list) else [expected]
        if not any(_check_type(value, t) for t in allowed):
            errors.append(f"{path}: expected {allowed}, got {type(value).__name__}")
            return errors

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: value {value!r} not in enum {schema['enum']!r}")

    if isinstance(value, str) and "maxLength" in schema and len(value) > schema["maxLength"]:
        errors.append(f"{path}: exceeds maximum length {schema['maxLength']}")

    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing required key {key!r}")
        props = schema.get("properties", {})
        for key, child_schema in props.items():
            if key in value:
                validate_shape(value[key], child_schema, f"{path}.{key}", errors)
        if schema.get("additionalProperties") is False:
            extras = set(value) - set(props)
            for key in sorted(extras):
                errors.append(f"{path}: unexpected key {key!r}")

    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(value):
            validate_shape(item, schema["items"], f"{path}[{i}]", errors)

    return errors


def validate_json_contract(value, schema: dict):
    ok, canonical, error = strict_json_roundtrip(value)
    if not ok:
        return False, None, [error]
    errors = validate_shape(canonical, schema)
    return (not errors), canonical if not errors else None, errors


SEARCH_ARGUMENT_SCHEMA = {
    "type": "object",
    "required": ["query"],
    "properties": {
        "query": {"type": "string"},
        "earliest_time": {"type": "string"},
        "latest_time": {"type": "string"},
        "max_count": {"type": "integer"},
        "output_format": {"type": "string", "enum": ["json", "csv", "xml"]},
    },
}


def validate_search_arguments(arguments):
    ok, canonical, errors = validate_json_contract(arguments, SEARCH_ARGUMENT_SCHEMA)
    if not ok:
        return False, None, errors
    if not canonical.get("query", "").strip():
        return False, None, ["$.query: query must be non-empty"]
    if "max_count" in canonical and not (1 <= canonical["max_count"] <= 100000):
        return False, None, ["$.max_count: must be between 1 and 100000"]
    return True, canonical, []


def validate_search_result_text(result_text):
    """Return a classification used before evidence/cache persistence."""
    ok, payload, error = parse_json_text(result_text, expected_type=dict, allow_embedded=False)
    if not ok:
        return {
            "ok": False,
            "kind": "malformed_json",
            "payload": None,
            "errors": [error],
        }

    if payload.get("error"):
        failure = classify_splunk_failure(payload)
        return {
            "ok": False,
            "kind": failure["kind"],
            "payload": payload,
            "errors": [failure["message"]],
            "failure": failure,
        }

    events = payload.get("events", payload.get("results"))
    if events is not None and not isinstance(events, list):
        return {
            "ok": False,
            "kind": "bad_result_shape",
            "payload": payload,
            "errors": ["events/results must be an array when present"],
        }

    event_count = payload.get("event_count")
    if event_count is not None:
        try:
            payload["event_count"] = int(event_count)
        except Exception:
            return {
                "ok": False,
                "kind": "bad_result_shape",
                "payload": payload,
                "errors": ["event_count must be integer-compatible"],
            }

    return {"ok": True, "kind": "ok", "payload": payload, "errors": []}


PLANNER_SCHEMA = {
    "type": "object",
    "required": [
        "investigation_type", "answer_contract", "primary_strategy",
        "check_strategy", "review_checks", "timeline_needed",
        "external_enrichment_needed", "stop_condition",
    ],
    "properties": {
        "investigation_type": {
            "type": "string",
            "enum": [
                "direct_lookup", "aggregation", "chronological", "timeline",
                "multi_step", "external_enrichment",
            ],
        },
        "answer_contract": {"type": "string"},
        "primary_strategy": {"type": "string"},
        "check_strategy": {"type": "string"},
        "review_checks": {"type": "array", "items": {"type": "string"}},
        "timeline_needed": {"type": "boolean"},
        "external_enrichment_needed": {"type": "boolean"},
        "stop_condition": {"type": "string"},
    },
}

VALIDATION_SCHEMA = {
    "type": "object",
    "required": [
        "decision", "confidence", "support_ids", "conflict_ids", "missing",
        "next_action", "recommended_spl", "summary",
    ],
    "properties": {
        "decision": {"type": "string", "enum": ["accept", "continue"]},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "support_ids": {"type": "array", "items": {"type": "string"}},
        "conflict_ids": {"type": "array", "items": {"type": "string"}},
        "missing": {"type": "array", "items": {"type": "string"}},
        "next_action": {"type": "string"},
        "recommended_spl": {"type": "string"},
        "summary": {"type": "string"},
    },
}


def parse_model_object(text, schema):
    ok, parsed, error = parse_json_text(text, expected_type=dict, allow_embedded=True)
    if not ok:
        return False, None, [error]
    return validate_json_contract(parsed, schema)


def is_failure_sentinel(value):
    if value is None:
        return False
    return str(value).strip().lower() in SUCCESS_SENTINELS
