"""Executable bidirectional timeline/case-map engine for ALR legacy compatibility.

The engine is evidence-backed and append-oriented. It never guesses an anchor: an
anchor timestamp must correspond to a stored case-map event. Relative positioning
is calculated from the current anchor at read time, so changing the anchor creates a
new version without rewriting historical event records.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from commander_agent.config import (
    TIMELINE_LEDGER_FILE,
    TIMELINE_ANCHOR_FILE,
    RELATIONSHIP_LEDGER_FILE,
    TIMELINE_SCHEMA_VERSION,
    MAX_TIMELINE_CONTEXT_CHARS,
)
from commander_agent.mcp.results import clip_text
from commander_agent.state.io import append_jsonl, json_load_file, json_write_atomic, stable_hash


def parse_event_time(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except Exception:
            return None
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        try:
            return datetime.fromtimestamp(float(text), tz=timezone.utc)
        except Exception:
            pass
    candidate = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(candidate)
    except Exception:
        # Common Splunk timestamp fallback.
        for fmt in ("%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except Exception:
                dt = None
        if dt is None:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso_utc(value):
    dt = parse_event_time(value)
    return dt.isoformat().replace("+00:00", "Z") if dt else None


def calculate_relative_position(event_time, anchor_time):
    event_dt = parse_event_time(event_time)
    anchor_dt = parse_event_time(anchor_time)
    if not event_dt or not anchor_dt:
        return None, None
    seconds = (event_dt - anchor_dt).total_seconds()
    if abs(seconds) < 0.000001:
        direction = "ANCHOR"
        seconds = 0.0
    elif seconds < 0:
        direction = "BEFORE"
    else:
        direction = "AFTER"
    return direction, seconds


def _load_jsonl(path):
    p = Path(path)
    if not p.exists():
        return []
    rows = []
    try:
        with p.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        rows.append(obj)
                except Exception:
                    continue
    except Exception:
        return []
    return rows


def load_timeline_rows():
    return [r for r in _load_jsonl(TIMELINE_LEDGER_FILE) if r.get("record_type") == "timeline"]


def _load_anchor_store():
    data = json_load_file(TIMELINE_ANCHOR_FILE, {})
    return data if isinstance(data, dict) else {}


def current_anchor(case_id):
    entry = _load_anchor_store().get(str(case_id), {})
    current = entry.get("current") if isinstance(entry, dict) else None
    return current if isinstance(current, dict) else None


def _matching_timeline_row(case_id, event_time, evidence_id=None, entity=None):
    wanted = iso_utc(event_time)
    if not wanted:
        return None, False
    rows = load_timeline_rows()

    # Prefer the current case.
    for row in rows:
        if row.get("case_id") != case_id:
            continue
        if evidence_id and row.get("evidence_id") != evidence_id:
            continue
        if iso_utc(row.get("event_time")) == wanted:
            return row, False

    # Map-first reuse: if the event is already mapped by an earlier case, allow the
    # new case to adopt it only when the timestamp plus optional entity uniquely
    # identify one prior row. This reuses evidence; it does not invent a new event.
    candidates = []
    entity_low = str(entity or "").strip().lower()
    for row in rows:
        if iso_utc(row.get("event_time")) != wanted:
            continue
        haystack = json.dumps(row, ensure_ascii=False).lower()
        if entity_low and entity_low not in haystack:
            continue
        candidates.append(row)
    if len(candidates) == 1:
        return candidates[0], True
    return None, False


def set_anchor(case_state, event_time, evidence_id=None, reason="", entity=None):
    """Set/version T0 only when the timestamp exists in stored evidence."""
    case_id = case_state.get("case_id")
    row, reused = _matching_timeline_row(
        case_id,
        event_time,
        evidence_id=evidence_id,
        entity=entity,
    )
    if not row:
        return {
            "ok": False,
            "error": (
                "Anchor rejected: timestamp is not uniquely present in stored timeline evidence "
                "for this case, or a reusable prior case-map event could not be uniquely identified."
            ),
        }

    if reused:
        adopted = dict(row)
        source_case = adopted.get("case_id")
        adopted["case_id"] = case_id
        adopted["reused_from_case"] = source_case
        adopted["event_id"] = stable_hash({
            "adopted_from": row.get("event_id"),
            "case_id": case_id,
            "event_time": wanted if (wanted := iso_utc(event_time)) else str(event_time),
        })[:20]
        adopted["created"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        append_jsonl(TIMELINE_LEDGER_FILE, adopted)
        row = adopted

    store = _load_anchor_store()
    entry = store.setdefault(case_id, {"history": []})
    history = entry.setdefault("history", [])
    previous = entry.get("current") or {}
    version = int(previous.get("anchor_version", 0)) + 1
    anchor = {
        "case_id": case_id,
        "anchor_version": version,
        "anchor_time_utc": iso_utc(event_time),
        "evidence_id": evidence_id or row.get("evidence_id"),
        "entity": entity or row.get("primary_entity") or row.get("secondary_entity"),
        "event_type": row.get("event_type"),
        "reason": str(reason or "Evidence-backed timeline anchor"),
        "created": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    history.append(anchor)
    entry["current"] = anchor
    store[case_id] = entry
    json_write_atomic(TIMELINE_ANCHOR_FILE, store)
    case_state["anchor"] = anchor
    return {"ok": True, "anchor": anchor}


def _try_json(value):
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return None
    try:
        return json.loads(value)
    except Exception:
        return None


def _flatten_target_values(value, out, prefix=""):
    """Extract useful target/resource values without dataset-specific answers."""
    interesting = (
        "username", "rolename", "groupname", "policyarn", "resource", "resourcearn",
        "bucket", "bucketname", "instanceid", "imageid", "functionname", "tablename",
        "keyid", "domain", "hostname", "url",
    )
    if isinstance(value, dict):
        for key, child in value.items():
            key_low = str(key).lower()
            next_prefix = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(child, (str, int, float)) and any(term in key_low for term in interesting):
                out.append((next_prefix, str(child)))
            else:
                _flatten_target_values(child, out, next_prefix)
    elif isinstance(value, list):
        for i, child in enumerate(value[:40]):
            _flatten_target_values(child, out, f"{prefix}[{i}]")


def extract_entities(event):
    """Return normalized entity dictionaries from a compact event."""
    entities = []

    def add(kind, value):
        if value is None:
            return
        text = str(value).strip()
        if not text:
            return
        item = {"type": kind, "value": text}
        if item not in entities:
            entities.append(item)

    add("access_key", event.get("accessKeyId") or event.get("userIdentity.accessKeyId"))
    # Generic credential-derivation support: a timeline event can relate an
    # original credential to a newly issued credential without knowing either
    # value in advance.
    add("access_key", event.get("derived_access_key") or event.get("responseElements.credentials.accessKeyId"))
    add("user", event.get("userName") or event.get("userIdentity.userName"))
    add("source_ip", event.get("sourceIPAddress") or event.get("src_ip") or event.get("src"))
    add("destination_ip", event.get("dest_ip") or event.get("dst") or event.get("destinationIPAddress"))
    add("host", event.get("host") or event.get("Computer") or event.get("hostname"))
    add("domain", event.get("domain") or event.get("query") or event.get("query_name"))
    add("event_type", event.get("eventName") or event.get("event_type") or event.get("action"))
    add("region", event.get("awsRegion"))
    add("url", event.get("url") or event.get("uri"))

    for key in ("requestParameters", "responseElements", "json"):
        nested = _try_json(event.get(key))
        if nested is not None:
            found = []
            _flatten_target_values(nested, found)
            for path, value in found[:24]:
                add("resource", value)

    # Extract URLs from excerpts when a dedicated field was not available.
    rawish = " ".join(str(event.get(k, "")) for k in ("content_excerpt", "_raw_excerpt", "message"))
    for url in re.findall(r"https?://[^\s<>\"']+", rawish, flags=re.IGNORECASE)[:12]:
        add("url", url.rstrip(".,);]"))
        try:
            host = urlparse(url).hostname
        except Exception:
            host = None
        if host:
            add("domain", host)

    return entities


def _first_entity(entities, *kinds):
    for kind in kinds:
        for entity in entities:
            if entity.get("type") == kind:
                return entity.get("value")
    return None


def timeline_row_from_event(case_state, evidence_record, event):
    timestamp = event.get("_time") or event.get("eventTime")
    if not timestamp or not parse_event_time(timestamp):
        return None
    entities = extract_entities(event)
    access_key = _first_entity(entities, "access_key")
    user = _first_entity(entities, "user")
    source_ip = _first_entity(entities, "source_ip")
    event_type = _first_entity(entities, "event_type")
    resource = _first_entity(entities, "resource")
    primary = access_key or user or source_ip or _first_entity(entities, "host", "domain", "resource")
    secondary = user if user and user != primary else source_ip if source_ip and source_ip != primary else resource

    anchor = case_state.get("anchor") or current_anchor(case_state.get("case_id")) or {}
    direction, seconds = calculate_relative_position(timestamp, anchor.get("anchor_time_utc"))
    return {
        "record_type": "timeline",
        "schema_version": TIMELINE_SCHEMA_VERSION,
        "event_id": stable_hash({
            "case_id": case_state.get("case_id"),
            "time": iso_utc(timestamp),
            "event_type": event_type,
            "primary": primary,
            "event": event,
        })[:20],
        "case_id": case_state.get("case_id"),
        "evidence_id": evidence_record.get("evidence_id"),
        "event_time": iso_utc(timestamp),
        "source": evidence_record.get("source"),
        "source_type": event.get("sourcetype") or event.get("_sourcetype"),
        "event_type": event_type,
        "primary_entity": primary,
        "secondary_entity": secondary,
        "access_key": access_key,
        "user": user,
        "source_ip": source_ip,
        "destination_ip": _first_entity(entities, "destination_ip"),
        "host": _first_entity(entities, "host"),
        "domain": _first_entity(entities, "domain"),
        "url": _first_entity(entities, "url"),
        "resource": resource,
        "derived_access_key": event.get("derived_access_key") or event.get("responseElements.credentials.accessKeyId"),
        "principal_id": event.get("principalId") or event.get("userIdentity.principalId"),
        "arn": event.get("arn") or event.get("userIdentity.arn"),
        "region": _first_entity(entities, "region"),
        "entities": entities,
        "anchor_version": anchor.get("anchor_version"),
        "anchor_time_utc": anchor.get("anchor_time_utc"),
        "direction_relative_anchor": direction,
        "seconds_from_anchor": seconds,
        "tool": evidence_record.get("tool"),
        "method_class": evidence_record.get("method_class"),
        "query": evidence_record.get("query"),
        "event": clip_text(json.dumps(event, ensure_ascii=False), 1600),
        "created": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def _relationship_records(row):
    """Emit evidence-scored co-occurrence edges with explicit provenance."""
    entities = [e for e in (row.get("entities", []) or []) if isinstance(e, dict) and e.get("type") and e.get("value")]

    def values(kind):
        out = []
        for e in entities:
            if e.get("type") == kind and e.get("value") not in out:
                out.append(e.get("value"))
        return out

    relationships = []
    provenance = {
        "evidence_id": row.get("evidence_id"), "source": row.get("source"),
        "tool": row.get("tool"), "method_class": row.get("method_class"),
        "query": row.get("query"), "event_id": row.get("event_id"), "event_time": row.get("event_time"),
    }

    def edge(src_type, src, relation, dst_type, dst, *, confidence, confidence_score, basis, sufficient_for_identity=False):
        if not src or not dst or src == dst:
            return
        relationships.append({
            "record_type": "relationship", "schema_version": TIMELINE_SCHEMA_VERSION,
            "case_id": row.get("case_id"), "event_id": row.get("event_id"), "evidence_id": row.get("evidence_id"),
            "event_time": row.get("event_time"), "from_type": src_type, "from": src,
            "relation": relation, "to_type": dst_type, "to": dst,
            "confidence": confidence, "confidence_score": float(confidence_score),
            "basis": basis, "sufficient_for_identity": bool(sufficient_for_identity),
            "provenance": provenance, "created": row.get("created"),
        })

    access_keys = values("access_key")
    derived = str(row.get("derived_access_key") or "")
    for i, access_key in enumerate(access_keys):
        for related_key in access_keys[i + 1:]:
            explicit_derivation = derived and derived in {access_key, related_key}
            kwargs = dict(confidence="high" if explicit_derivation else "medium",
                          confidence_score=0.99 if explicit_derivation else 0.65,
                          basis="explicit_same_event_credential_derivation" if explicit_derivation else "same_event_credential_cooccurrence",
                          sufficient_for_identity=bool(explicit_derivation))
            edge("access_key", access_key, "RELATED_CREDENTIAL", "access_key", related_key, **kwargs)
            edge("access_key", related_key, "RELATED_CREDENTIAL", "access_key", access_key, **kwargs)

    for access_key in access_keys:
        for user in values("user"):
            edge("access_key", access_key, "USED_BY", "user", user, confidence="high", confidence_score=0.97,
                 basis="same_event_cloudtrail_identity", sufficient_for_identity=True)
        for source_ip in values("source_ip"):
            edge("access_key", access_key, "SEEN_FROM", "source_ip", source_ip, confidence="medium", confidence_score=0.60,
                 basis="same_event_network_indicator", sufficient_for_identity=False)
        for action in values("event_type"):
            edge("access_key", access_key, "PERFORMED", "event_type", action, confidence="high", confidence_score=0.90,
                 basis="same_event_api_action", sufficient_for_identity=False)

    for user in values("user"):
        for source_ip in values("source_ip"):
            edge("user", user, "SEEN_FROM", "source_ip", source_ip, confidence="medium", confidence_score=0.55,
                 basis="same_event_network_indicator", sufficient_for_identity=False)
        for action in values("event_type"):
            edge("user", user, "PERFORMED", "event_type", action, confidence="high", confidence_score=0.88,
                 basis="same_event_api_action", sufficient_for_identity=False)
    for source_ip in values("source_ip"):
        for action in values("event_type"):
            edge("source_ip", source_ip, "CALLED", "event_type", action, confidence="medium", confidence_score=0.50,
                 basis="same_event_network_indicator", sufficient_for_identity=False)
    for action in values("event_type"):
        for resource in values("resource"):
            edge("event_type", action, "TARGETED", "resource", resource, confidence="high", confidence_score=0.85,
                 basis="same_event_request_resource", sufficient_for_identity=False)
    for url in values("url"):
        for domain in values("domain"):
            edge("url", url, "HOSTED_BY", "domain", domain, confidence="high", confidence_score=0.95,
                 basis="parsed_url_hostname", sufficient_for_identity=False)

    unique, seen = [], set()
    for rel in relationships:
        key = (rel["from_type"], rel["from"], rel["relation"], rel["to_type"], rel["to"], rel["event_id"])
        if key not in seen:
            seen.add(key); unique.append(rel)
    return unique

def append_timeline_events(case_state, evidence_record, events):
    count = 0
    rows = []
    for event in events:
        if not isinstance(event, dict):
            continue
        row = timeline_row_from_event(case_state, evidence_record, event)
        if not row:
            continue
        append_jsonl(TIMELINE_LEDGER_FILE, row)
        for relation in _relationship_records(row):
            append_jsonl(RELATIONSHIP_LEDGER_FILE, relation)
        rows.append(row)
        count += 1
    return count, rows


def maybe_auto_anchor(case_state, evidence_record, rows):
    """Auto-anchor only when evidence is unambiguous (exactly one timestamped row)."""
    if case_state.get("anchor") or current_anchor(case_state.get("case_id")):
        return None
    if len(rows) != 1:
        return None
    if evidence_record.get("method_class") not in {"chronological_first", "targeted_rows", "timeline_anchor"}:
        return None
    row = rows[0]
    return set_anchor(
        case_state,
        row.get("event_time"),
        evidence_id=row.get("evidence_id"),
        reason="Automatically anchored because targeted evidence contained exactly one timestamped event.",
        entity=row.get("primary_entity"),
    )


def _position_with_current_anchor(row):
    result = dict(row)
    anchor = current_anchor(row.get("case_id"))
    if anchor:
        direction, seconds = calculate_relative_position(row.get("event_time"), anchor.get("anchor_time_utc"))
        result["anchor_version"] = anchor.get("anchor_version")
        result["anchor_time_utc"] = anchor.get("anchor_time_utc")
        result["direction_relative_anchor"] = direction
        result["seconds_from_anchor"] = seconds
    return result


def query_timeline(terms=None, case_id=None, limit=20, entity=None, start_time=None, end_time=None, order="relevance_desc"):
    """Query the persisted case map without allowing presentation limits to alter chronology.

    ALR ranked newest rows first and sliced before chronological consumers such as
    ``timeline_map`` and ``timeline_after`` re-sorted the result.  On populations larger
    than the presentation limit that silently changed the meaning of FIRST/BEFORE/AFTER.

    ALR and later therefore apply the requested ordering to the complete filtered population
    *before* limiting it.  ``limit=None`` is reserved for internal timeline helpers that
    must apply a temporal predicate before their own presentation limit.
    """
    terms = [str(t).lower() for t in (terms or []) if str(t).strip()]
    start_dt = parse_event_time(start_time) if start_time else None
    end_dt = parse_event_time(end_time) if end_time else None
    entity_low = str(entity).lower().strip() if entity else ""
    scored = []

    for raw in load_timeline_rows():
        if case_id and raw.get("case_id") != case_id:
            continue
        row = _position_with_current_anchor(raw)
        dt = parse_event_time(row.get("event_time"))
        if start_dt and (not dt or dt < start_dt):
            continue
        if end_dt and (not dt or dt > end_dt):
            continue
        haystack = json.dumps(row, ensure_ascii=False).lower()
        if entity_low and entity_low not in haystack:
            continue
        score = sum(1 for term in terms if term in haystack) if terms else 1
        if score:
            scored.append((score, dt or datetime.min.replace(tzinfo=timezone.utc), row))

    if order == "chronological_asc":
        scored.sort(key=lambda x: (x[1], -x[0], str(x[2].get("event_id") or "")))
    elif order == "chronological_desc":
        scored.sort(key=lambda x: (x[1], x[0], str(x[2].get("event_id") or "")), reverse=True)
    else:
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)

    rows = [row for _, _, row in scored]
    if limit is None:
        return rows
    return rows[: max(1, min(int(limit), 5000))]


def timeline_before(case_id, minutes=30, entity=None, limit=50):
    anchor = current_anchor(case_id)
    if not anchor:
        return {"ok": False, "error": "No anchor exists for this case.", "events": []}
    window = abs(float(minutes)) * 60.0
    rows = []
    for row in query_timeline(case_id=case_id, entity=entity, limit=None, order="chronological_asc"):
        seconds = row.get("seconds_from_anchor")
        if isinstance(seconds, (int, float)) and -window <= seconds < 0:
            rows.append(row)
    rows.sort(key=lambda r: r.get("seconds_from_anchor", 0))
    return {"ok": True, "anchor": anchor, "events": rows[-max(1, min(int(limit), 100)):]}


def timeline_after(case_id, minutes=30, entity=None, limit=50):
    anchor = current_anchor(case_id)
    if not anchor:
        return {"ok": False, "error": "No anchor exists for this case.", "events": []}
    window = abs(float(minutes)) * 60.0
    rows = []
    for row in query_timeline(case_id=case_id, entity=entity, limit=None, order="chronological_asc"):
        seconds = row.get("seconds_from_anchor")
        if isinstance(seconds, (int, float)) and 0 < seconds <= window:
            rows.append(row)
    rows.sort(key=lambda r: r.get("seconds_from_anchor", 0))
    return {"ok": True, "anchor": anchor, "events": rows[: max(1, min(int(limit), 100))]}


def timeline_between(start_time, end_time, case_id=None, entity=None, limit=100):
    return query_timeline(
        case_id=case_id,
        entity=entity,
        start_time=start_time,
        end_time=end_time,
        limit=limit,
        order="chronological_asc",
    )


def timeline_entities(case_id=None, limit=30):
    counts = {}
    for row in load_timeline_rows():
        if case_id and row.get("case_id") != case_id:
            continue
        for entity in row.get("entities", []) or []:
            if not isinstance(entity, dict):
                continue
            kind, value = entity.get("type"), entity.get("value")
            if kind and value:
                label = f"{kind}:{value}"
                counts[label] = counts.get(label, 0) + 1
    ranked = sorted(counts.items(), key=lambda x: (-x[1], x[0]))
    return [{"entity": label, "count": count} for label, count in ranked[: max(1, min(int(limit), 100))]]


def timeline_related(entity, case_id=None, limit=50):
    target = str(entity or "").strip().lower()
    if not target:
        return []
    aggregate = {}
    for rel in _load_jsonl(RELATIONSHIP_LEDGER_FILE):
        if case_id and rel.get("case_id") != case_id:
            continue
        if target not in str(rel.get("from", "")).lower() and target not in str(rel.get("to", "")).lower():
            continue
        key = (rel.get("from_type"), rel.get("from"), rel.get("relation"), rel.get("to_type"), rel.get("to"))
        entry = aggregate.setdefault(key, {**rel, "count": 0, "event_ids": [], "provenance_records": [],
                                           "confidence_score": 0.0, "sufficient_for_identity": False})
        entry["count"] += 1
        entry["confidence_score"] = max(float(entry.get("confidence_score") or 0), float(rel.get("confidence_score") or 0))
        if rel.get("sufficient_for_identity"):
            entry["sufficient_for_identity"] = True
        if rel.get("event_id") and rel["event_id"] not in entry["event_ids"]:
            entry["event_ids"].append(rel["event_id"])
        prov = rel.get("provenance")
        if isinstance(prov, dict) and prov not in entry["provenance_records"]:
            entry["provenance_records"].append(prov)
    for entry in aggregate.values():
        score = float(entry.get("confidence_score") or 0)
        entry["confidence"] = "high" if score >= 0.85 else "medium" if score >= 0.55 else "low"
    rows = sorted(aggregate.values(), key=lambda r: (-float(r.get("confidence_score") or 0), -r.get("count", 0), str(r.get("relation", ""))))
    return rows[: max(1, min(int(limit), 100))]

def timeline_map(case_id=None, entity=None, limit=100):
    # Chronology is established across the complete filtered population before the
    # display limit is applied.  This is safe for FIRST semantics even with hundreds
    # or thousands of persisted events.
    rows = query_timeline(case_id=case_id, entity=entity, limit=limit, order="chronological_asc")
    return {
        "case_id": case_id,
        "anchor": current_anchor(case_id) if case_id else None,
        "events": rows,
        "entities": timeline_entities(case_id=case_id, limit=40),
        "relationships": timeline_related(entity, case_id=case_id, limit=40) if entity else [],
    }


def _terms(text):
    stop = {
        "what", "when", "where", "which", "this", "that", "with", "from",
        "using", "user", "most", "first", "does", "into", "attempt", "resource",
    }
    out = []
    for token in re.findall(r"[a-z0-9_.:/-]+", (text or "").lower()):
        if len(token) >= 4 and token not in stop and token not in out:
            out.append(token)
    return out[:24]


def timeline_context_for_question(question, limit=10):
    rows = query_timeline(terms=_terms(question), limit=limit)
    if not rows:
        return ""
    lines = []
    for row in rows:
        relative = ""
        if row.get("seconds_from_anchor") is not None:
            relative = f" {row.get('direction_relative_anchor')} {row.get('seconds_from_anchor'):+.0f}s"
        lines.append(
            " | ".join(
                str(x)
                for x in (
                    row.get("event_time", "") + relative,
                    row.get("source_type", ""),
                    row.get("event_type", ""),
                    row.get("primary_entity", ""),
                    row.get("secondary_entity", ""),
                    row.get("case_id", ""),
                    row.get("evidence_id", ""),
                )
                if x
            )
            + "\n"
            + clip_text(row.get("event", ""), 650)
        )
    return clip_text("\n\n".join(lines), MAX_TIMELINE_CONTEXT_CHARS)
