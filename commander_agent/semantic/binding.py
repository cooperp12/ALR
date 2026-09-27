"""Resolve Investigation Frame concepts to semantic field/source roles.

Bindings are domain tradecraft. They never contain benchmark answers and are chosen
before result values are available, preventing answer-seeking metric selection.
"""
from __future__ import annotations

from commander_agent.semantic.catalog import fields_for_role, field_meta
from commander_agent.state.io import stable_hash


def _binding(frame, *, concept, role, field=None, kind="field", value=None, reason="", confidence="high", alternatives=None):
    core = {
        "concept": concept, "domain": frame.get("domain"), "role_required": role,
        "field": field, "kind": kind, "value": value, "status": "SUPPORTED",
        "reason": reason, "confidence": confidence, "alternatives": list(alternatives or []),
    }
    fp = stable_hash(core)
    return {**core, "binding_id": "SB-" + fp[:12], "fingerprint": fp}


def _first(domain, role):
    xs = fields_for_role(domain, role)
    return xs[0] if xs else None


def resolve_semantic_bindings(frame, question=""):
    if not isinstance(frame, dict): return []
    domain = frame.get("domain")
    out = []
    # Source/domain binding is explicit state even when the physical source is supplied by a skill.
    if domain == "aws_cloudtrail":
        out.append(_binding(frame, concept="source", role="event_source", kind="source", value="aws_cloudtrail", reason="Question concerns AWS API activity; CloudTrail is the event domain selected by the active AWS skill."))
    elif domain == "email":
        out.append(_binding(frame, concept="source", role="message_source", kind="source", value="email", reason="Question concerns a notification/message and requires a communication source."))
    elif domain == "mixed":
        out.append(_binding(frame, concept="source", role="multi_source", kind="source", value="email_then_external", reason="Question requires a communication artifact followed by an external reference."))

    actor = str(frame.get("actor_concept") or "").lower()
    if domain == "aws_cloudtrail" and "access key" in actor:
        f = _first(domain, "actor_credential")
        out.append(_binding(frame, concept="actor", role="actor_credential", field=f, kind="group_field", reason=field_meta(domain,f).get("description","")))

    for scope in frame.get("scope_concepts") or []:
        low = str(scope).lower()
        if domain == "aws_cloudtrail" and "iam" in low:
            out.append(_binding(frame, concept="scope:IAM service activity", role="service_scope", field="eventSource", kind="filter", value="iam.amazonaws.com", reason="IAM service activity is represented by the CloudTrail service-scope field."))
        if domain == "aws_cloudtrail" and "failed" in low:
            out.append(_binding(frame, concept="scope:failed requests", role="failure_presence", field="errorCode", kind="presence_filter", value="present", reason="CloudTrail errorCode presence is used to identify failed requests; it is a population filter, not necessarily the measured error representation."))

    role = frame.get("measure_role")
    concept = frame.get("measure_concept") or frame.get("requested_output_concept")
    if domain == "aws_cloudtrail" and role == "detailed_failure_description":
        f = _first(domain, role)
        alts = fields_for_role(domain, "failure_category")
        out.append(_binding(frame, concept=concept or "error", role=role, field=f, kind="measurement_field", alternatives=alts, reason="The requested quantity is a distinct error description, so bind the measure to the detailed failure-description role rather than the broader failure category."))
    elif domain == "aws_cloudtrail" and role == "failure_category":
        f = _first(domain, role)
        out.append(_binding(frame, concept=concept or "error", role=role, field=f, kind="measurement_field", reason="The question explicitly asks for error codes/categories/types, so use the categorical failure role."))
    elif domain == "aws_cloudtrail" and role == "client_identifier":
        f = _first(domain, role)
        out.append(_binding(frame, concept="requested output", role=role, field=f, kind="output_field", reason=field_meta(domain,f).get("description","")))
    elif domain == "aws_cloudtrail" and role == "target_resource":
        f = _first(domain, "request_target_container")
        out.append(_binding(frame, concept="requested output", role="request_target_container", field=f, kind="output_container", reason=field_meta(domain,f).get("description","")))
    elif domain == "aws_cloudtrail" and role == "external_identifier":
        f = _first(domain, "ami_id") or _first(domain, "external_identifier")
        out.append(_binding(frame, concept="requested output", role="external_identifier", field=f, kind="output_field", reason=field_meta(domain,f).get("description","")))

    if domain == "aws_cloudtrail" and frame.get("event_concept"):
        out.append(_binding(frame, concept="event", role="api_operation", field="eventName", kind="event_filter", value=frame.get("event_concept"), reason="CloudTrail eventName identifies the API operation; the concept value remains subject to query-time resolution when phrased naturally."))
    if domain == "aws_cloudtrail" and frame.get("temporal_constraint") in {"earliest","latest"}:
        out.append(_binding(frame, concept="time", role="event_time", field="_time", kind="order_field", value=frame.get("temporal_constraint"), reason="Splunk _time is used for chronological selection."))

    # Dedupe by fingerprint.
    seen=set(); dedup=[]
    for b in out:
        if b["fingerprint"] in seen: continue
        seen.add(b["fingerprint"]); dedup.append(b)
    return dedup


def binding_for(bindings, *, kind=None, concept=None, role=None):
    for b in bindings or []:
        if b.get("status") != "SUPPORTED": continue
        if kind is not None and b.get("kind") != kind: continue
        if concept is not None and b.get("concept") != concept: continue
        if role is not None and b.get("role_required") != role: continue
        return dict(b)
    return None


def compact_bindings(bindings):
    lines=[]
    for b in bindings or []:
        target = b.get("field") or b.get("value") or "[unbound]"
        lines.append(f"- {b.get('binding_id')}: {b.get('concept')} -> {target} ({b.get('kind')}, role={b.get('role_required')}, status={b.get('status')})")
    return "\n".join(lines)


def semantic_binding_query_violations(query, bindings):
    """Block measurement/group drift before a measurement contract exists.

    ALR legacy compatibility applies this universally, not only inside semantic recovery. It recognises
    both direct dc(field) measurements and values(field)-based independent checks.
    """
    import re
    q=str(query or '')
    stats = re.search(r'\|\s*stats\b([^|]*)', q, re.I)
    if not stats:
        return []
    body = stats.group(1)
    dc_fields=[x.strip() for x in re.findall(r'\bdc\s*\(\s*([^\)]+?)\s*\)', body, re.I)]
    values_fields=[x.strip() for x in re.findall(r'\bvalues\s*\(\s*([^\)]+?)\s*\)', body, re.I)]
    if not dc_fields and not values_fields:
        return []

    measure=binding_for(bindings,kind='measurement_field')
    group=binding_for(bindings,kind='group_field')
    errs=[]
    if measure and measure.get('field'):
        required=str(measure['field']).strip().lower()
        measured=[x.lower() for x in (dc_fields + values_fields)]
        # If a query is aggregating a failure representation, at least one aggregate
        # must implement the active measurement field. Schema inspections can include
        # several fields; they pass as long as the bound field is among them.
        if measured and required not in measured:
            errs.append(f"Active semantic binding requires measurement of {measure['field']} (distinct_count/values)")
    if group and group.get('field'):
        m=re.search(r'\bBY\s+([^|]+)',body,re.I)
        if m and str(group['field']).lower() not in m.group(1).lower():
            errs.append(f"Active semantic binding requires grouping by {group['field']}")
    return errs
