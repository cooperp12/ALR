"""Investigation Frame: concept-level parsing before schema/query binding.

The frame intentionally contains no physical Splunk field names and no benchmark
answers. Models interpret the question; Python validates/normalises the result and
provides a deterministic fallback when the model is unavailable.
"""
from __future__ import annotations

import json
import re

from commander_agent.mcp.results import clip_text
from commander_agent.reasoning.local_ollama import structured_chat, message_content
from commander_agent.skills.structured_output_validation.scripts.schema import parse_model_object

PRIMITIVES = [
    "SOURCE_SELECT", "SCOPE_RESOLVE", "SCHEMA_DISCOVER", "FIELD_ROLE_RESOLVE",
    "ENTITY_FILTER", "EVENT_FILTER", "DIRECT_EXTRACT", "AGGREGATE", "RANK",
    "TEMPORAL_SELECT", "RELATIONSHIP_PIVOT", "REFERENCE_FOLLOW",
    "EXTERNAL_ENRICH", "INDEPENDENT_VALIDATE",
]
ROLES = [
    "none", "detailed_failure_description", "failure_category", "api_operation",
    "client_identifier", "target_resource", "external_identifier", "identifier",
    "content_value", "event_count", "unknown",
]
SCHEMA = {
    "type": "object",
    "required": [
        "domain", "source_concepts", "scope_concepts", "actor_concept", "event_concept",
        "measure_concept", "measure_role", "aggregation", "ranking", "temporal_constraint",
        "requested_output_concept", "requires_reference_follow", "requires_external_enrichment",
        "primitives", "confidence",
    ],
    "additionalProperties": False,
    "properties": {
        "domain": {"type": "string", "enum": ["aws_cloudtrail", "email", "mixed", "unknown"]},
        "source_concepts": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
        "scope_concepts": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        "actor_concept": {"type": ["string", "null"]},
        "event_concept": {"type": ["string", "null"]},
        "measure_concept": {"type": ["string", "null"]},
        "measure_role": {"type": "string", "enum": ROLES},
        "aggregation": {"type": "string", "enum": ["none", "distinct_count", "count", "first", "latest"]},
        "ranking": {"type": "string", "enum": ["none", "maximum", "minimum"]},
        "temporal_constraint": {"type": "string", "enum": ["none", "earliest", "latest", "before_after"]},
        "requested_output_concept": {"type": "string"},
        "requires_reference_follow": {"type": "boolean"},
        "requires_external_enrichment": {"type": "boolean"},
        "primitives": {"type": "array", "items": {"type": "string", "enum": PRIMITIVES}, "maxItems": 12},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
}


def _has(low, *terms): return any(t in low for t in terms)


def heuristic_frame(question: str):
    """Generic fallback based on investigative concepts, never expected answers."""
    q = str(question or "")
    low = q.lower()
    aws = _has(low, "aws", "iam", "access key", "ec2", "cloud image", "ami", "user agent")
    notification = _has(low, "notification", "support case", "email", "message")
    leaked_secret = _has(low, "secret access key", "external code repository") and not _has(low, "support case")

    frame = {
        "domain": "aws_cloudtrail" if aws else ("email" if notification else "unknown"),
        "source_concepts": [], "scope_concepts": [], "actor_concept": None, "event_concept": None,
        "measure_concept": None, "measure_role": "none", "aggregation": "none", "ranking": "none",
        "temporal_constraint": "none", "requested_output_concept": "requested_value",
        "requires_reference_follow": False, "requires_external_enrichment": False,
        "primitives": ["SOURCE_SELECT"], "confidence": "medium", "frame_source": "deterministic_fallback",
    }
    if leaked_secret:
        frame["domain"] = "mixed"
        frame["source_concepts"] = ["incident notification or repository reference", "external code repository"]
        frame["requested_output_concept"] = "secret_access_key"
        frame["requires_reference_follow"] = True
        frame["primitives"] += ["DIRECT_EXTRACT", "REFERENCE_FOLLOW", "DIRECT_EXTRACT"]
    elif notification and not _has(low, "user agent", "create a key", "cloud image", "distinct"):
        frame["domain"] = "email"
        frame["source_concepts"] = ["AWS notification email"]
        frame["requested_output_concept"] = "support_case_id"
        frame["primitives"] += ["DIRECT_EXTRACT"]
    if aws and frame["domain"] != "email":
        frame["source_concepts"] = ["AWS CloudTrail activity"]
    if "iam" in low:
        frame["scope_concepts"].append("IAM service activity")
    if _has(low, "access key", "accesskey"):
        frame["actor_concept"] = "IAM user access key"
    if _has(low, "most distinct", "distinct errors", "unique errors", "different errors"):
        frame.update({
            "measure_concept": "error", "aggregation": "distinct_count", "ranking": "maximum",
            "requested_output_concept": "actor credential",
            "measure_role": "failure_category" if _has(low, "error code", "errorcode", "error category", "error type") else "detailed_failure_description",
        })
        frame["scope_concepts"].append("failed requests")
        frame["primitives"] += ["SCOPE_RESOLVE", "FIELD_ROLE_RESOLVE", "AGGREGATE", "RANK", "INDEPENDENT_VALIDATE"]
    if _has(low, "user agent", "application that originated", "client"):
        frame["requested_output_concept"] = "client identifier"
        frame["measure_role"] = "client_identifier"
        frame["event_concept"] = "describe account" if "describe" in low else None
        frame["primitives"] += ["ENTITY_FILTER", "EVENT_FILTER", "FIELD_ROLE_RESOLVE", "DIRECT_EXTRACT", "INDEPENDENT_VALIDATE"]
    if _has(low, "create a key", "createaccesskey"):
        frame["event_concept"] = "CreateAccessKey"
        frame["requested_output_concept"] = "target resource"
        frame["measure_role"] = "target_resource"
        frame["primitives"] += ["ENTITY_FILTER", "EVENT_FILTER", "FIELD_ROLE_RESOLVE", "DIRECT_EXTRACT", "INDEPENDENT_VALIDATE"]
    if _has(low, "launch", "runinstances", "cloud image"):
        frame["event_concept"] = "RunInstances"
        frame["requested_output_concept"] = "operating system codename" if "codename" in low else "image identifier"
        frame["measure_role"] = "external_identifier"
        frame["requires_external_enrichment"] = "codename" in low or "version" in low
        frame["primitives"] += ["EVENT_FILTER", "FIELD_ROLE_RESOLVE", "DIRECT_EXTRACT"]
        if _has(low, "first attempt", "earliest", "initial attempt"):
            frame["temporal_constraint"] = "earliest"; frame["aggregation"] = "first"; frame["primitives"].append("TEMPORAL_SELECT")
        if frame["requires_external_enrichment"]: frame["primitives"].append("EXTERNAL_ENRICH")
        frame["primitives"].append("INDEPENDENT_VALIDATE")
    # Stable de-duplication preserving order.
    frame["primitives"] = list(dict.fromkeys(frame["primitives"]))
    frame["scope_concepts"] = list(dict.fromkeys(frame["scope_concepts"]))
    return frame


def _prompt(question: str):
    return f"""You are the Investigation Frame stage for a SOC investigation.
Interpret the user's investigative concepts BEFORE choosing physical fields or writing SPL.

Rules:
- Do not answer the question.
- Do not output Splunk field names, sourcetypes, query syntax, identifiers, or guessed values.
- Separate source/scope/actor/event/measurement/output concepts.
- 'Failure population' and 'what is being measured' may be different concepts.
- If the user asks for distinct errors without explicitly saying error codes/categories, use the semantic role detailed_failure_description; if they explicitly ask for error codes/categories/types, use failure_category.
- Use primitives to describe the generic workflow, not benchmark-specific steps.

QUESTION:
{question}
"""


def build_investigation_frame(question: str, metrics=None):
    fallback = heuristic_frame(question)
    if metrics is not None: metrics["investigation_frame_calls"] = metrics.get("investigation_frame_calls", 0) + 1
    try:
        response = structured_chat(
            role="investigator",
            messages=[{"role": "user", "content": _prompt(question)}],
            schema=SCHEMA,
            metrics=metrics,
            purpose="investigation_frame",
            think="low",
        )
        ok, obj, errs = parse_model_object(message_content(response), SCHEMA)
        if ok:
            obj = dict(obj); obj["frame_source"] = "local_model"
            # ALR legacy compatibility relationship reconciliation: the model may interpret nuance, but it
            # cannot contradict structural facts deterministically present in the question.
            # This prevents spurious REFERENCE_FOLLOW/temporal/event routes from bypassing
            # the bound aggregation path (the exact failure seen in ALR legacy compatibility Q3).
            # Deterministic relationship reconciliation applies to any structural fact
            # clearly present in the wording, not only aggregation questions.
            if fallback.get("requested_output_concept") in {"support_case_id", "client identifier"}:
                obj["domain"] = fallback.get("domain") or obj.get("domain")
                obj["source_concepts"] = list(fallback.get("source_concepts") or obj.get("source_concepts") or [])
                obj["requested_output_concept"] = fallback.get("requested_output_concept")
                obj["aggregation"] = fallback.get("aggregation", "none")
                obj["ranking"] = fallback.get("ranking", "none")
                obj["requires_reference_follow"] = bool(fallback.get("requires_reference_follow"))
                obj["requires_external_enrichment"] = bool(fallback.get("requires_external_enrichment"))
                if fallback.get("actor_concept"):
                    obj["actor_concept"] = fallback.get("actor_concept")
                if fallback.get("event_concept"):
                    obj["event_concept"] = fallback.get("event_concept")
            if fallback.get("aggregation") == "distinct_count":
                obj["domain"] = fallback.get("domain") or obj.get("domain")
                obj["actor_concept"] = fallback.get("actor_concept") or obj.get("actor_concept")
                obj["measure_concept"] = fallback.get("measure_concept") or obj.get("measure_concept")
                obj["requested_output_concept"] = fallback.get("requested_output_concept") or obj.get("requested_output_concept")
                obj["aggregation"] = "distinct_count"
                obj["ranking"] = "maximum"
                obj["event_concept"] = fallback.get("event_concept")
                obj["temporal_constraint"] = fallback.get("temporal_constraint", "none")
                obj["requires_reference_follow"] = bool(fallback.get("requires_reference_follow"))
                obj["requires_external_enrichment"] = bool(fallback.get("requires_external_enrichment"))
                obj["scope_concepts"] = list(dict.fromkeys((obj.get("scope_concepts") or []) + (fallback.get("scope_concepts") or [])))
            if fallback.get("measure_role") not in {None, "", "none", "unknown"}:
                obj["measure_role"] = fallback["measure_role"]
            primitives = list(dict.fromkeys((obj.get("primitives") or []) + (fallback.get("primitives") or [])))
            if not obj.get("requires_reference_follow"):
                primitives = [x for x in primitives if x != "REFERENCE_FOLLOW"]
            if not obj.get("requires_external_enrichment"):
                primitives = [x for x in primitives if x != "EXTERNAL_ENRICH"]
            obj["primitives"] = primitives
            return obj
        if metrics is not None: metrics["investigation_frame_failures"] = metrics.get("investigation_frame_failures", 0) + 1
        print("[Investigation frame fallback: " + clip_text("; ".join(errs), 320) + "]")
    except Exception as exc:
        if metrics is not None: metrics["investigation_frame_failures"] = metrics.get("investigation_frame_failures", 0) + 1
        print(f"[Investigation frame fallback: {type(exc).__name__}: {clip_text(str(exc),260)}]")
    return fallback


def render_investigation_frame(frame):
    if not isinstance(frame, dict): return "[none]"
    keys = ["domain","source_concepts","scope_concepts","actor_concept","event_concept","measure_concept","measure_role","aggregation","ranking","temporal_constraint","requested_output_concept","requires_reference_follow","requires_external_enrichment","primitives","confidence","frame_source"]
    return json.dumps({k: frame.get(k) for k in keys}, ensure_ascii=False, indent=2)
