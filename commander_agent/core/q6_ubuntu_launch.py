from __future__ import annotations

import json
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
from urllib.parse import urlparse
import re
import time
import urllib.request
from pathlib import Path
from typing import Any

from commander_agent.config import BOTSV3_EARLIEST, BOTSV3_LATEST, PROJECT_ROOT
from commander_agent.enrichment.web import (
    fetch_public_url,
    validate_public_url,
    web_search_public,
)
from commander_agent.mcp.adapter import tool_result_to_text
from commander_agent.core.q6_anomaly_review import review_q6_anomaly
from commander_agent.skills.structured_output_validation.scripts.schema import validate_search_result_text
from commander_agent.state.sequence import (
    build_validated_sequence_state, render_validated_sequence_state, sequence_values,
)
from commander_agent.state.io import json_write_atomic
from commander_agent.state.timeline import (
    append_timeline_events,
    parse_event_time,
    query_timeline,
    query_timeline,
    set_anchor,
    timeline_after,
    timeline_map,
    timeline_related,
)

# Q6 intentionally contains no built-in Ubuntu release/codename catalogue.
# The AMI -> release and release -> codename links must both come from external
# evidence discovered during the run.
CANONICAL_LOCATOR = "https://cloud-images.ubuntu.com/locator/ec2/releasesTable"
Q6_LEDGER = PROJECT_ROOT / "ALR_q6_resolution.jsonl"
Q6_STAGE_TRACE = PROJECT_ROOT / "ALR_q6_stage_trace.json"
CANONICAL_STREAMS = (
    "https://cloud-images.ubuntu.com/releases/streams/v1/com.ubuntu.cloud:released:aws.json",
)
Q6_DYNAMIC_WINDOW_MINUTES = (5, 15, 30, 60, 180, 360)
Q6_SCOPE_STABILITY_REQUIRED = 2


def build_sequence_state(results: list[dict[str, Any]]) -> dict[str, Any]:
    return build_validated_sequence_state(results)


def _write_stage_trace(record: dict[str, Any]) -> None:
    try:
        json_write_atomic(Q6_STAGE_TRACE, {
            "kind": record.get("kind"), "status": record.get("status"),
            "stage": record.get("stage"), "reason": record.get("reason"),
            "timeline_case_id": record.get("timeline_case_id"),
            "stage_trace": record.get("stage_trace", []),
        })
    except Exception:
        pass


def _trace_stage(record: dict[str, Any], stage: str, status: str, **details) -> None:
    record.setdefault("stage_trace", []).append({
        "stage": stage, "status": status,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        **details,
    })
    _write_stage_trace(record)


def _review_anomaly(record: dict[str, Any] | None, metrics: dict, kind: str, packet: dict[str, Any]):
    review = review_q6_anomaly(kind, packet, metrics)
    if record is not None:
        record.setdefault("anomaly_reviews", []).append(review)
        _trace_stage(
            record,
            "anomaly_review",
            "complete",
            kind=kind,
            deterministic_action=review.get("deterministic_action"),
            model_status=review.get("model_status"),
            worker_agrees=review.get("worker_agrees_with_default"),
            supervisor_agrees=review.get("supervisor_agrees_with_default"),
        )
    return review


def extract_access_key(sequence_state: dict[str, Any]) -> str | None:
    candidates = sequence_values(sequence_state, "aws_access_key") + [
        sequence_state.get("compromised_access_key"),
        sequence_state.get("Q3"), sequence_state.get("q3_answer"),
    ]
    for candidate in reversed(candidates):
        match = re.search(r"\bAKIA[A-Z0-9]{16}\b", str(candidate or ""))
        if match:
            return match.group(0)
    return None


def render_sequence_state(sequence_state: dict[str, Any]) -> str:
    return render_validated_sequence_state(sequence_state)

def _append_jsonl(record: dict[str, Any]) -> None:
    try:
        with Q6_LEDGER.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _rows_from_result_text(result_text):
    validation = validate_search_result_text(result_text)
    if not validation.get("ok"):
        return [], "; ".join(str(x) for x in validation.get("errors", ["Invalid response"]))
    payload = validation.get("payload") or {}
    if "events" not in payload and "results" not in payload:
        return [], "Malformed response: missing events/results array (not an empty search)"
    rows = payload.get("events", payload.get("results"))
    if not isinstance(rows, list) or any(not isinstance(x, dict) for x in rows):
        return [], "Malformed response: events/results must contain objects"
    return rows, None


def _scalar(value):
    if isinstance(value, list):
        values = list(dict.fromkeys(str(x) for x in value if x is not None))
        return values[0] if len(values) == 1 else ""
    return str(value) if value is not None else ""


def _normalise_launch_row(row):
    raw = row.get("_raw", {})
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    identity = raw.get("userIdentity") or {}
    request = raw.get("requestParameters") or {}
    items = (request.get("instancesSet") or {}).get("items") or []
    if isinstance(items, dict):
        items = [items]
    images = [x.get("imageId") for x in items if isinstance(x, dict) and x.get("imageId")]
    if request.get("imageId"):
        images.append(request["imageId"])
    def get(*names, default=None):
        return next((_scalar(row[n]) for n in names if _scalar(row.get(n))), _scalar(default))
    out = {
        "event_time": get("eventTime", "event_time", default=raw.get("eventTime")),
        "region": get("region", "awsRegion", default=raw.get("awsRegion")),
        "image_id": get("image_id", "imageId", "requestParameters.instancesSet.items{}.imageId", "requestParameters.imageId", default=images),
        "iam_user": get("iam_user", "userName", "userIdentity.userName", default=identity.get("userName")),
        "access_key": get("access_key", "accessKeyId", "userIdentity.accessKeyId", default=identity.get("accessKeyId")),
        "event_id": get("eventID", "event_id", default=raw.get("eventID")),
        "user_agent": get("userAgent", "user_agent", default=raw.get("userAgent")),
        "error_code": get("errorCode", "error_code", default=raw.get("errorCode")),
        "account_id": get("account_id", default=identity.get("accountId")),
        "event_name": get("eventName", default=raw.get("eventName")),
        "event_source": get("eventSource", default=raw.get("eventSource")),
    }
    try:
        out["epoch"] = datetime.fromisoformat(out["event_time"].replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except (ValueError, TypeError):
        out["epoch"] = None
    return out



def _normalise_identity_row(row):
    raw = row.get("_raw", {})
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    identity = raw.get("userIdentity") or {}
    response = raw.get("responseElements") or {}
    credentials = response.get("credentials") or {} if isinstance(response, dict) else {}

    def get(*names, default=None):
        return next((_scalar(row[n]) for n in names if _scalar(row.get(n))), _scalar(default))

    derived = get(
        "derived_access_key",
        "responseElements.credentials.accessKeyId",
        default=credentials.get("accessKeyId") if isinstance(credentials, dict) else None,
    )
    out = {
        "event_time": get("eventTime", "event_time", default=raw.get("eventTime")),
        "event_name": get("eventName", default=raw.get("eventName")),
        "event_source": get("eventSource", default=raw.get("eventSource")),
        "source_ip": get("sourceIPAddress", "source_ip", default=raw.get("sourceIPAddress")),
        "access_key": get("access_key", "accessKeyId", "userIdentity.accessKeyId", default=identity.get("accessKeyId")),
        "iam_user": get("iam_user", "userName", "userIdentity.userName", default=identity.get("userName")),
        "iam_arn": get("iam_arn", "userIdentity.arn", default=identity.get("arn")),
        "account_id": get("account_id", "userIdentity.accountId", default=identity.get("accountId")),
        "principal_id": get("principal_id", "userIdentity.principalId", default=identity.get("principalId")),
        "identity_type": get("identity_type", "userIdentity.type", default=identity.get("type")),
        "derived_access_key": derived,
        "error_code": get("errorCode", "error_code", default=raw.get("errorCode")),
    }
    try:
        out["epoch"] = datetime.fromisoformat(out["event_time"].replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except (ValueError, TypeError):
        out["epoch"] = None
    return out


def _identity_query(access_key):
    # Start from the evidence-validated leaked key, then derive the stable IAM
    # identity and any STS credentials minted by that identity.  Q6 asks for the
    # compromised IAM *user*, not necessarily API calls that continue to carry
    # the original long-term AKIA key.
    return (
        'index=botsv3 earliest=0 latest=now sourcetype="aws:cloudtrail" '
        + f'"{access_key}"'
        + ' | spath'
        + ' | spath path=userIdentity.accessKeyId output=access_key'
        + ' | spath path=userIdentity.userName output=iam_user'
        + ' | spath path=userIdentity.arn output=iam_arn'
        + ' | spath path=userIdentity.accountId output=account_id'
        + ' | spath path=userIdentity.principalId output=principal_id'
        + ' | spath path=responseElements.credentials.accessKeyId output=derived_access_key'
        + f' | where access_key="{access_key}"'
        + ' | sort 0 + _time'
        + ' | head 1001'
        + ' | table _time eventTime eventName eventSource sourceIPAddress access_key iam_user iam_arn account_id principal_id userIdentity.type derived_access_key errorCode _raw'
    )


def _derive_identity_graph(rows, original_key):
    events = [_normalise_identity_row(row) for row in rows]
    events = [e for e in events if e.get("access_key") == original_key]
    users = sorted({e["iam_user"] for e in events if e.get("iam_user")})
    arns = sorted({e["iam_arn"] for e in events if e.get("iam_arn")})
    account_ids = sorted({e["account_id"] for e in events if e.get("account_id")})
    principal_ids = sorted({e["principal_id"] for e in events if e.get("principal_id")})
    source_ips = sorted({e["source_ip"] for e in events if e.get("source_ip")})
    derived_keys = sorted({
        e["derived_access_key"] for e in events
        if re.fullmatch(r"ASIA[A-Z0-9]{16}", e.get("derived_access_key") or "")
        and not e.get("error_code")
    })
    relationship_events = [
        e for e in events
        if e.get("event_name") in {"GetCallerIdentity", "GetSessionToken"}
        or e.get("derived_access_key")
    ]
    basis_events = [e for e in relationship_events if e.get("epoch") is not None] or [e for e in events if e.get("epoch") is not None]
    basis_epochs = [e["epoch"] for e in basis_events]
    scope_start = min(basis_epochs) if basis_epochs else None
    scope_end = max(basis_epochs) if basis_epochs else None
    common = {
        "events": events, "iam_users": users, "derived_access_keys": derived_keys,
        "arns": arns, "account_ids": account_ids, "principal_ids": principal_ids,
        "source_ips": source_ips, "relationship_events": relationship_events,
        "scope_basis_start_epoch": scope_start, "scope_basis_end_epoch": scope_end,
        "scope_basis_event_count": len(basis_events),
    }
    if len(users) > 1:
        return {"status": "ambiguous", "reason": "The validated long-term key maps to multiple IAM usernames; automatic user pivot is unsafe.", **common}
    if not users and not derived_keys:
        return {"status": "insufficient", "reason": "The validated key did not expose a stable IAM username or successful derived STS access key.", **common}
    if scope_start is None or scope_end is None:
        return {"status": "insufficient", "reason": "The related identity evidence has no valid eventTime, so an evidence-based temporal scope cannot be established.", **common}
    return {
        "status": "resolved", "original_access_key": original_key,
        "iam_user": users[0] if len(users) == 1 else "", "all_access_keys": [original_key] + [k for k in derived_keys if k != original_key],
        **common,
    }


def _temporal_window(identity, minutes):
    start = identity.get("scope_basis_start_epoch")
    end = identity.get("scope_basis_end_epoch")
    if start is None or end is None:
        return None
    padding = abs(float(minutes)) * 60.0
    return {
        "minutes": float(minutes),
        "start_epoch": float(start) - padding,
        "end_epoch": float(end) + padding,
        "basis_start_epoch": float(start),
        "basis_end_epoch": float(end),
        "basis_event_count": int(identity.get("scope_basis_event_count") or 0),
    }

def _spl_string(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"') + '"'


def _identity_predicate(identity):
    terms = []
    user = str(identity.get("iam_user") or "").strip()
    if user:
        terms.append(f'iam_user={_spl_string(user)}')
    for key in identity.get("all_access_keys") or []:
        if key:
            terms.append(f'access_key={_spl_string(key)}')
    # Stable deterministic order and no duplicate predicates.
    return "(" + " OR ".join(dict.fromkeys(terms)) + ")"


def _row_matches_identity(row, identity):
    user = str(identity.get("iam_user") or "")
    keys = set(identity.get("all_access_keys") or [])
    return bool((user and row.get("iam_user") == user) or (row.get("access_key") in keys))


def _identity_window_clause(identity, field="event_epoch", window=None):
    window = window or identity.get("selected_temporal_window") or {}
    start = window.get("start_epoch")
    end = window.get("end_epoch")
    if start is None or end is None:
        return ""
    return f"{field}>={int(start)} AND {field}<={int(end)}"


def _primary_query_for_identity(identity, window=None):
    predicate = _identity_predicate(identity)
    window_clause = _identity_window_clause(identity, window=window)
    return (
        'index=botsv3 earliest=0 latest=now sourcetype="aws:cloudtrail"'
        + ' | spath'
        + ' | spath path=userIdentity.accessKeyId output=access_key'
        + ' | spath path=userIdentity.userName output=iam_user'
        + ' | spath path=requestParameters.instancesSet.items{}.imageId output=nested_image'
        + ' | spath path=requestParameters.imageId output=direct_image'
        + ' | eval image_id=coalesce(nested_image,direct_image), region=awsRegion'
        + ' | eval event_epoch=strptime(eventTime,"%Y-%m-%dT%H:%M:%SZ")'
        + ' | where eventSource="ec2.amazonaws.com" AND eventName="RunInstances" AND ' + predicate
        + (' AND ' + window_clause if window_clause else '')
        + ' | eventstats count AS candidate_count'
        + ' | sort 0 + event_epoch + eventID'
        + ' | head 1001'
        + ' | table _time eventTime eventID region access_key iam_user userAgent image_id errorCode candidate_count _raw'
    )


def _raw_launch_query(identity, window=None):
    tokens = ["RunInstances"]
    if identity.get("iam_user"):
        tokens.append(str(identity["iam_user"]))
    tokens.extend(identity.get("all_access_keys") or [])
    keyword_group = "(" + " OR ".join(_spl_string(x) for x in dict.fromkeys(tokens[1:])) + ")" if len(tokens) > 1 else ""
    window_clause = _identity_window_clause(identity, field="_time", window=window)
    return (
        'index=botsv3 earliest=0 latest=now sourcetype="aws:cloudtrail" "RunInstances" '
        + keyword_group
        + ((' | where ' + window_clause) if window_clause else '')
        + ' | head 1001 | table _raw _time'
    )


def _raw_boundary_verification_query(identity, window=None):
    """Independent chronology check using raw-event time reduction only.

    The primary raw recovery parses all candidate JSON in Python.  This verifier
    instead asks Splunk to establish the minimum raw ``_time`` boundary without
    relying on the historical nested-field ``spath`` route, then Python validates
    every event tied at that boundary.
    """
    tokens = ["RunInstances"]
    if identity.get("iam_user"):
        tokens.append(str(identity["iam_user"]))
    tokens.extend(identity.get("all_access_keys") or [])
    keyword_group = "(" + " OR ".join(_spl_string(x) for x in dict.fromkeys(tokens[1:])) + ")" if len(tokens) > 1 else ""
    window_clause = _identity_window_clause(identity, field="_time", window=window)
    return (
        'index=botsv3 earliest=0 latest=now sourcetype="aws:cloudtrail" "RunInstances" '
        + keyword_group
        + ((' | where ' + window_clause) if window_clause else '')
        + ' | eventstats min(_time) AS first_raw_time'
        + ' | where _time=first_raw_time'
        + ' | sort 0 + _raw'
        + ' | head 1001'
        + ' | table _raw _time first_raw_time'
    )


def _verification_query_for_identity(identity, window=None):
    predicate = _identity_predicate(identity)
    window_clause = _identity_window_clause(identity, window=window)
    return (
        'index=botsv3 earliest=0 latest=now sourcetype="aws:cloudtrail"'
        + ' | spath'
        + ' | spath path=userIdentity.accessKeyId output=access_key'
        + ' | spath path=userIdentity.userName output=iam_user'
        + ' | spath path=requestParameters.instancesSet.items{}.imageId output=nested_image'
        + ' | spath path=requestParameters.imageId output=direct_image'
        + ' | eval image_id=coalesce(nested_image,direct_image), region=awsRegion'
        + ' | eval event_epoch=strptime(eventTime,"%Y-%m-%dT%H:%M:%SZ")'
        + ' | where eventSource="ec2.amazonaws.com" AND eventName="RunInstances" AND ' + predicate
        + (' AND ' + window_clause if window_clause else '')
        + ' | eventstats min(event_epoch) AS first_epoch'
        + ' | where event_epoch=first_epoch'
        + ' | sort 0 + eventID | head 1001'
        + ' | table _time eventTime eventID region access_key iam_user userAgent image_id errorCode _raw'
    )

def _earliest_signature(launches):
    valid = [x for x in launches if x.get("epoch") is not None]
    if not valid:
        return None
    first_epoch = min(x["epoch"] for x in valid)
    rows = [x for x in valid if x["epoch"] == first_epoch]
    fields = ("event_id", "epoch", "region", "image_id", "iam_user", "access_key")
    return tuple(sorted(tuple(x.get(k) for k in fields) for x in rows))


async def _dynamic_launch_search(session, identity, *, record=None, metrics=None):
    """Adaptive temporal search with extraction-route health and bounded failover.

    ALR legacy compatibility spent multiple expansions on a parsed CloudTrail route that returned
    zero rows even though equivalent raw evidence existed.  ALR legacy compatibility probes raw
    evidence whenever a parsed expansion is empty.  If raw matching launches are
    present, the parsed route is marked unhealthy, worker/supervisor anomaly review
    is invoked, and subsequent expansions stay on the evidence-producing raw route.
    """
    history = []
    windows = tuple(Q6_DYNAMIC_WINDOW_MINUTES) or (30,)
    previous_signature = None
    stable_expansions = 0
    last_with_candidates = None
    active_route = "parsed_identity"

    def normalise(rows, route):
        launches = [_normalise_launch_row(row) for row in rows]
        if route == "parsed_identity":
            launches = [row for row in launches if row.get("event_source") in ("", "ec2.amazonaws.com")
                        and row.get("event_name") in ("", "RunInstances") and _row_matches_identity(row, identity)]
        else:
            launches = [row for row in launches if row.get("event_source") == "ec2.amazonaws.com"
                        and row.get("event_name") == "RunInstances" and _row_matches_identity(row, identity)]
        return launches

    for minutes in windows:
        window = _temporal_window(identity, minutes)

        if active_route == "parsed_identity":
            rows, error = await _splunk_search(
                session, _primary_query_for_identity(identity, window=window), record=record, metrics=metrics
            )
            if error:
                return None, None, None, active_route, history, error
            if len(rows) >= 1001:
                return None, rows, window, active_route, history, "Temporal-scope query reached its result limit"
            launches = normalise(rows, active_route)
            counted = [int(str(r.get("candidate_count"))) for r in rows if str(r.get("candidate_count") or "").isdigit()]
            if counted and max(counted) > len(launches):
                return None, rows, window, active_route, history, "Tool returned only part of the counted candidate set"
            signature = _earliest_signature(launches)
            entry = {"route": active_route, "minutes": minutes, "window": window,
                     "candidate_count": len(launches), "earliest_signature": signature}
            history.append(entry)

            if launches:
                last_with_candidates = (launches, rows, window)
                if previous_signature is not None and signature == previous_signature:
                    stable_expansions += 1
                else:
                    stable_expansions = 0
                previous_signature = signature
                entry["stable_expansions"] = stable_expansions
                if Q6_SCOPE_STABILITY_REQUIRED <= 0 or stable_expansions >= Q6_SCOPE_STABILITY_REQUIRED:
                    return launches, rows, window, active_route, history, None
                continue

            # Parsed extraction says zero. Probe the equivalent raw population at
            # the same evidence-derived scope before wasting wider parsed searches.
            raw_rows, raw_error = await _splunk_search(
                session, _raw_launch_query(identity, window=window), record=record, metrics=metrics
            )
            if raw_error:
                return None, None, None, "raw_identity_recovery", history, raw_error
            if len(raw_rows) >= 1001:
                return None, raw_rows, window, "raw_identity_recovery", history, "Raw temporal-scope query reached its result limit"
            raw_launches = normalise(raw_rows, "raw_identity_recovery")
            raw_signature = _earliest_signature(raw_launches)
            raw_entry = {"route": "raw_identity_probe", "minutes": minutes, "window": window,
                         "candidate_count": len(raw_launches), "earliest_signature": raw_signature}
            history.append(raw_entry)
            if not raw_launches:
                continue

            # Route contradiction: machine evidence exists but the structured
            # extractor returned none.  Models review the anomaly only; Python
            # deterministically switches to the working raw evidence route.
            packet = {
                "window": window,
                "parsed_candidate_count": 0,
                "raw_candidate_count": len(raw_launches),
                "raw_earliest_signature": raw_signature,
                "identity": {
                    "iam_user_present": bool(identity.get("iam_user")),
                    "access_key_count": len(identity.get("all_access_keys") or []),
                },
            }
            review = _review_anomaly(record, metrics, "source_route_disagreement", packet)
            if record is not None:
                record["route_health"] = {
                    "parsed_identity": "failed_extraction_zero_vs_raw_matches",
                    "raw_identity_recovery": "healthy",
                    "switch_window_minutes": minutes,
                }
            metrics["q6_route_health_failovers"] = metrics.get("q6_route_health_failovers", 0) + 1
            active_route = "raw_identity_recovery"
            previous_signature = raw_signature
            stable_expansions = 0
            last_with_candidates = (raw_launches, raw_rows, window)
            raw_entry["route_switch"] = True
            raw_entry["stable_expansions"] = 0
            if Q6_SCOPE_STABILITY_REQUIRED <= 0:
                return raw_launches, raw_rows, window, active_route, history, None
            continue

        # Raw route after a demonstrated structured-extraction disagreement.
        rows, error = await _splunk_search(
            session, _raw_launch_query(identity, window=window), record=record, metrics=metrics
        )
        if error:
            return None, None, None, active_route, history, error
        if len(rows) >= 1001:
            return None, rows, window, active_route, history, "Raw temporal-scope query reached its result limit"
        launches = normalise(rows, active_route)
        signature = _earliest_signature(launches)
        entry = {"route": active_route, "minutes": minutes, "window": window,
                 "candidate_count": len(launches), "earliest_signature": signature}
        history.append(entry)
        if not launches:
            continue
        last_with_candidates = (launches, rows, window)
        if previous_signature is not None and signature == previous_signature:
            stable_expansions += 1
        else:
            stable_expansions = 0
        previous_signature = signature
        entry["stable_expansions"] = stable_expansions
        if Q6_SCOPE_STABILITY_REQUIRED <= 0 or stable_expansions >= Q6_SCOPE_STABILITY_REQUIRED:
            return launches, rows, window, active_route, history, None

    if last_with_candidates and stable_expansions >= max(1, Q6_SCOPE_STABILITY_REQUIRED - 1):
        launches, rows, window = last_with_candidates
        history[-1]["accepted_at_max_scope"] = True
        return launches, rows, window, active_route, history, None
    if last_with_candidates:
        label = "raw" if active_route == "raw_identity_recovery" else "parsed"
        return None, last_with_candidates[1], last_with_candidates[2], active_route, history, f"Earliest {label} launch changed as temporal scope expanded; chronology is not stable"
    return [], [], _temporal_window(identity, windows[-1]), active_route, history, None


def _primary_query(access_key, *, broad=False):
    # Raw key match avoids depending on automatic nested-field extraction.
    return (
        'index=botsv3 earliest=0 latest=now sourcetype="aws:cloudtrail" '
        + f'"{access_key}"'
        + ' | spath'
        + ' | spath path=userIdentity.accessKeyId output=access_key'
        + ' | spath path=userIdentity.userName output=iam_user'
        + ' | spath path=requestParameters.instancesSet.items{}.imageId output=nested_image'
        + ' | spath path=requestParameters.imageId output=direct_image'
        + f' | where access_key="{access_key}" AND eventSource="ec2.amazonaws.com" AND eventName="RunInstances"'
        + ' | eval image_id=coalesce(nested_image,direct_image), region=awsRegion'
        + ' | eval event_epoch=strptime(eventTime,"%Y-%m-%dT%H:%M:%SZ")'
        + ' | eventstats count AS candidate_count'
        + ' | sort 0 + event_epoch + eventID'
        + ' | head 1001'
        + ' | table _time eventTime eventID region access_key iam_user userAgent image_id errorCode candidate_count _raw'
    )


def _verification_query(access_key):
    # Independently derive the earliest set, retaining ties and missing images.
    query = _primary_query(access_key).split(' | eventstats count AS candidate_count')[0]
    return (query + ' | eventstats min(event_epoch) AS first_epoch'
            + ' | where event_epoch=first_epoch | sort 0 + eventID | head 1001'
            + ' | table _time eventTime eventID region access_key iam_user userAgent image_id errorCode _raw')


async def _splunk_search(session, query, max_count=1001, *, record=None, metrics=None):
    args = {"query": query, "earliest_time": "0", "latest_time": "now",
            "max_count": max_count, "output_format": "json"}
    metrics = metrics if metrics is not None else {}
    for key in ("search_calls", "search_attempts", "tool_calls"):
        metrics[key] = metrics.get(key, 0) + 1
    metrics.setdefault("queries", []).append(query)
    print("\nQ6 SEARCH (all historical data): " + query)
    try:
        result = await session.call_tool("search_oneshot", arguments=args)
        if getattr(result, "isError", False):
            rows, error = [], "MCP returned isError"
        else:
            rows, error = _rows_from_result_text(tool_result_to_text(result))
    except Exception as exc:
        rows, error = [], f"MCP search failed: {type(exc).__name__}: {exc}"
    diagnostic = {"arguments": args, "row_count": len(rows), "error": error,
                  "fields": sorted(set().union(*(r.keys() for r in rows))) if rows else [],
                  "normalised_rows": [_normalise_launch_row(r) for r in rows],
                  "raw_sha256": [hashlib.sha256(str(r.get("_raw", "")).encode()).hexdigest() for r in rows]}
    if record is not None:
        record.setdefault("searches", []).append(diagnostic)
    print(f"Q6 RESULT: rows={len(rows)}; error={error or 'none'}")
    return rows, error


def _evidence_units(text, *, require_ami=None, region=None):
    units = re.split(r"[\n\r]|(?<=[.!?])\s+", text or "")
    if require_ami:
        exact = re.compile(r"(?<![a-z0-9-])" + re.escape(require_ami) + r"(?![a-z0-9-])", re.I)
        units = [u for u in units if exact.search(u)]
    for unit in units:
        if len(unit) > 1200:
            continue
        if region and region.lower() not in unit.lower():
            continue
        if require_ami and len(set(re.findall(r"ami-[0-9a-f]+", unit, re.I))) != 1:
            continue
        yield " ".join(unit.split())


def _release_from_text(text, *, require_ami=None, region=None):
    """Extract only an evidenced Ubuntu release descriptor.

    No codename catalogue is consulted here.  A version and/or series must be
    stated in the same bounded evidence unit as the exact AMI (and region when
    supplied).
    """
    matches = []
    for unit in _evidence_units(text, require_ami=require_ami, region=region):
        low = unit.lower()
        if "ubuntu" not in low:
            continue
        versions = sorted(set(re.findall(r"(?<![0-9.])(\d{2}\.\d{2})(?![0-9.])", unit)))
        series = sorted(set(x.lower() for x in re.findall(
            r"(?:ubuntu(?:/images[^ ]*)?[-_/])([a-z][a-z0-9-]{2,24})[-_/](?:\d{2}\.\d{2})",
            unit, re.I
        )))
        if not series:
            series = sorted(set(x.lower() for x in re.findall(
                r"(?:series|release(?:_name)?|suite|codename)\s*[=:>\"']+\s*([a-z][a-z0-9-]{2,24})",
                unit, re.I
            )))
        if len(versions) > 1 or len(series) > 1 or (not versions and not series):
            continue
        matches.append({
            "version": versions[0] if versions else "",
            "series": series[0] if series else "",
            "evidence_excerpt": unit,
        })
    keys = {(m["version"], m["series"]) for m in matches}
    if len(keys) != 1:
        return None
    result = dict(matches[0])
    result.pop("evidence_excerpt", None)
    return result


def _codename_from_text(text, *, version="", series=""):
    """Extract a two-word Ubuntu codename from bounded official release text.

    This parser contains only structural patterns.  It has no release-to-codename
    mapping and therefore cannot manufacture a benchmark answer locally.
    """
    candidates = []
    version_pat = re.escape(version) if version else r"\d{2}\.\d{2}"
    for unit in re.split(r"[\n\r]|(?<=[.!?])\s+", text or ""):
        unit = " ".join(unit.split())
        if len(unit) > 1800 or "ubuntu" not in unit.lower():
            continue
        if version and not re.search(r"(?<![0-9.])" + version_pat + r"(?![0-9.])", unit):
            continue
        ubuntu_product = r"Ubuntu(?:\s+(?:Server|Desktop|Cloud|Core))?\s+"
        patterns = [
            ubuntu_product + version_pat + r"(?:\.\d+)?(?:\s+LTS)?\s*\(([A-Z][A-Za-z-]+\s+[A-Z][A-Za-z-]+)\)",
            r"[\"“]([A-Z][A-Za-z-]+\s+[A-Z][A-Za-z-]+)[\"”]\s+is\s+(?:the\s+)?code\s*name\s+for\s+Ubuntu\s+" + version_pat,
            r"Ubuntu[^.]{0,160}\bcodename\s+([A-Z][A-Za-z-]+\s+[A-Z][A-Za-z-]+)",
        ]
        for pat in patterns:
            for value in re.findall(pat, unit, re.I):
                value = " ".join(str(value).split())
                if len(value.split()) == 2:
                    candidates.append((value, unit))
        if series:
            # Series can strengthen a match when the AMI resolver already exposed
            # it, but version evidence is sufficient when the official release page
            # itself states the two-word codename.
            candidates = [(c, u) for c, u in candidates if series.lower() in u.lower() or version]
    unique = {}
    for value, unit in candidates:
        unique.setdefault(value.lower(), (value, unit))
    if len(unique) != 1:
        return None
    value, unit = next(iter(unique.values()))
    return {"codename": value, "evidence_excerpt": unit}


def _official_codename_urls(version: str) -> list[str]:
    """Construct official Ubuntu release pages from an evidenced version only."""
    version = str(version or "").strip()
    if not re.fullmatch(r"\d{2}\.\d{2}", version):
        return []
    slug = version.replace(".", "-")
    return [
        f"https://releases.ubuntu.com/{version}/",
        f"https://cloud-images.ubuntu.com/releases/{version}/release/",
        f"https://ubuntu.com/{slug}",
    ]


def _official_codename_direct_lookup(version: str, series: str = "") -> dict[str, Any] | None:
    """Resolve codename from deterministic official Ubuntu pages.

    The URLs are derived from the externally evidenced release version; no local
    codename catalogue or challenge-specific value is consulted.  Multiple
    reachable official pages must agree if more than one yields a codename.
    """
    matches = []
    attempted = []
    for url in _official_codename_urls(version):
        attempted.append(url)
        try:
            final_url, text = _fetch_evidence_blocks(url)
        except Exception:
            continue
        found = _codename_from_text(text, version=version, series=series)
        if not found:
            continue
        matches.append({
            "codename": found["codename"],
            "source": "Official Ubuntu release metadata",
            "url": final_url,
            "excerpt": found["evidence_excerpt"],
        })
    if not matches:
        return None
    grouped = {}
    for item in matches:
        grouped.setdefault(item["codename"].lower(), []).append(item)
    if len(grouped) != 1:
        return None
    items = next(iter(grouped.values()))
    result = dict(items[0])
    result.update({
        "source_tier": "official_direct_release_page",
        "confidence": "very-high" if len(items) >= 2 else "high",
        "attempted_urls": attempted,
        "corroborating_sources": [
            {"source": x["source"], "url": x["url"]} for x in items
        ],
    })
    return result


def resolve_ubuntu_codename(release: dict[str, Any]) -> dict[str, Any] | None:
    version = str((release or {}).get("version") or "").strip()
    series = str((release or {}).get("series") or "").strip().lower()
    if not version and not series:
        return None

    # Prefer deterministic official endpoints constructed from the release version.
    # This removes search-engine availability/ranking from the common path while
    # preserving the contamination boundary: the codename exists only on fetched
    # external evidence.
    if version:
        direct = _official_codename_direct_lookup(version, series)
        if direct:
            return direct

    # Search remains a fallback for releases whose direct historical pages are not
    # reachable.  Only official Ubuntu hosts are accepted.
    queries = []
    if version:
        queries.extend([
            f'site:wiki.ubuntu.com Ubuntu "{version}" release notes codename',
            f'site:ubuntu.com Ubuntu "{version}" codename',
        ])
    if series:
        queries.append(f'site:wiki.ubuntu.com Ubuntu "{series}" release notes')
    seen_urls = set()
    for query in queries:
        try:
            payload = json.loads(web_search_public(query))
        except Exception:
            continue
        for item in (payload.get("results") or [])[:8]:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            host = (urlparse(url).hostname or "").lower()
            if not (host == "ubuntu.com" or host.endswith(".ubuntu.com")):
                continue
            try:
                final_url, text = _fetch_evidence_blocks(url)
            except Exception:
                continue
            found = _codename_from_text(text, version=version, series=series)
            if found:
                return {
                    "codename": found["codename"],
                    "source": str(item.get("title") or final_url),
                    "url": final_url,
                    "query": query,
                    "excerpt": found["evidence_excerpt"],
                    "confidence": "high",
                    "source_tier": "official_search_fallback",
                    "attempted_urls": _official_codename_urls(version),
                }
    return None

def _canonical_locator_lookup(ami_id: str, region: str) -> dict[str, Any] | None:
    try:
        validate_public_url(CANONICAL_LOCATOR)
        req = urllib.request.Request(
            CANONICAL_LOCATOR,
            headers={"User-Agent": "Mozilla/5.0 BOTSv3-ALR legacy compatibility/1.0", "Accept": "application/json,text/plain,*/*"},
        )
        with urllib.request.urlopen(req, timeout=20) as response:
            body = response.read(15_000_000).decode("utf-8", errors="replace")
    except Exception:
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    rows = data.get("aaData", data.get("data", [])) if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return None
    matches = []
    for row in rows:
        excerpt = json.dumps(row, ensure_ascii=False)
        release = _release_from_text(excerpt, require_ami=ami_id, region=region)
        if release:
            matches.append((release, excerpt))
    if not matches or len({x[0]["version"] for x in matches}) != 1:
        return None
    release, excerpt = matches[0]
    return {"release": release, "source": "Canonical Ubuntu EC2 image locator",
            "url": CANONICAL_LOCATOR, "excerpt": excerpt, "confidence": "high"}


class _EvidenceBlocks(HTMLParser):
    """Preserve row/paragraph boundaries that the generic text fetch flattens."""
    def __init__(self):
        super().__init__()
        self.blocks, self.current, self.skip = [], [], 0
    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        if tag in ("tr", "p", "li", "pre", "div"):
            self.flush()
    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        if tag in ("tr", "p", "li", "pre", "div"):
            self.flush()
    def handle_data(self, data):
        if not self.skip:
            self.current.append(data)
    def flush(self):
        value = " ".join(" ".join(self.current).split())
        if value:
            self.blocks.append(value)
        self.current = []


def _fetch_evidence_blocks(url):
    validate_public_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as response:
        final_url = response.geturl()
        validate_public_url(final_url)
        text = response.read(2_000_000).decode("utf-8", errors="replace")
        content_type = response.headers.get("Content-Type", "")
    if "html" in content_type:
        parser = _EvidenceBlocks()
        parser.feed(text)
        parser.flush()
        return final_url, "\n".join(parser.blocks)
    return final_url, text


def _json_nodes_containing(value, needle, path=()):
    out = []
    if isinstance(value, dict):
        serial = json.dumps(value, ensure_ascii=False)
        if needle.lower() in serial.lower():
            out.append((path, value))
        for key, child in value.items():
            if isinstance(child, (dict, list)):
                out.extend(_json_nodes_containing(child, needle, path + (str(key),)))
    elif isinstance(value, list):
        for i, child in enumerate(value):
            if isinstance(child, (dict, list)):
                out.extend(_json_nodes_containing(child, needle, path + (str(i),)))
    return out


def _canonical_stream_lookup(ami_id: str, region: str) -> dict[str, Any] | None:
    for url in CANONICAL_STREAMS:
        try:
            validate_public_url(url)
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 BOTSv3-ALR legacy compatibility/1.0", "Accept": "application/json,*/*"})
            with urllib.request.urlopen(req, timeout=20) as response:
                final_url = response.geturl()
                validate_public_url(final_url)
                text = response.read(20_000_000).decode("utf-8", errors="replace")
            data = json.loads(text)
        except Exception:
            continue
        matches = []
        for path, node in _json_nodes_containing(data, ami_id):
            excerpt = " / ".join(path) + " " + json.dumps(node, ensure_ascii=False)
            release = _release_from_text(excerpt, require_ami=ami_id, region=region)
            if release:
                matches.append((release, excerpt))
        keys = {(m[0].get("version", ""), m[0].get("series", "")) for m in matches}
        if len(keys) == 1:
            release, excerpt = matches[0]
            return {"release": release, "source": "Canonical Ubuntu public cloud image stream",
                    "url": final_url, "excerpt": excerpt, "confidence": "high", "source_tier": "canonical_stream"}
    return None


def _search_release_candidates(queries, ami_id, region, *, official_only=False):
    candidates = []
    seen_urls = set()
    for query in queries:
        try:
            payload = json.loads(web_search_public(query))
        except Exception:
            continue
        for item in (payload.get("results") or [])[:8]:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            host = (urlparse(url).hostname or "").lower()
            official = host == "ubuntu.com" or host.endswith(".ubuntu.com")
            if official_only and not official:
                continue
            try:
                final_url, text = _fetch_evidence_blocks(url)
            except Exception:
                continue
            release = _release_from_text(text, require_ami=ami_id, region=region)
            if not release:
                continue
            excerpt = next((u for u in re.split(r"[\n\r]|(?<=[.!?])\s+", text)
                            if _release_from_text(u, require_ami=ami_id, region=region)), "")
            candidates.append({
                "release": release, "source": str(item.get("title") or url), "url": final_url,
                "query": query, "excerpt": excerpt, "host": host,
                "confidence": "high" if official else "medium",
            })
    return candidates


def _authoritative_search_lookup(ami_id: str, region: str) -> dict[str, Any] | None:
    queries = [
        f'site:cloud-images.ubuntu.com "{ami_id}" "{region}"',
        f'site:ubuntu.com "{ami_id}" "{region}" Ubuntu',
    ]
    candidates = _search_release_candidates(queries, ami_id, region, official_only=True)
    keys = {(c["release"].get("version", ""), c["release"].get("series", "")) for c in candidates}
    if len(keys) == 1 and candidates:
        result = dict(candidates[0]); result["source_tier"] = "authoritative_search"; return result
    return None


def _corroborated_search_lookup(ami_id: str, region: str) -> dict[str, Any] | None:
    queries = [
        f'"{ami_id}" "{region}" Ubuntu',
        f'"{ami_id}" Canonical Ubuntu AMI',
    ]
    candidates = _search_release_candidates(queries, ami_id, region, official_only=False)
    grouped = {}
    for c in candidates:
        key = (c["release"].get("version", ""), c["release"].get("series", ""))
        grouped.setdefault(key, []).append(c)
    winners = []
    for key, items in grouped.items():
        domains = {x.get("host") for x in items if x.get("host")}
        if len(domains) >= 2:
            winners.append((key, items))
    if len(winners) != 1:
        return None
    _, items = winners[0]
    result = dict(items[0])
    result.update({
        "source_tier": "corroborated_web",
        "confidence": "medium-high",
        "corroborating_sources": [{"source": x["source"], "url": x["url"], "host": x["host"]} for x in items],
    })
    return result


def resolve_ubuntu_ami(ami_id: str, region: str) -> dict[str, Any] | None:
    """Resolve exact regional AMI through ordered evidence tiers.

    No tier may relax the exact AMI+region requirement.  General web evidence is
    accepted only when at least two independent domains corroborate one release.
    """
    result = _canonical_locator_lookup(ami_id, region)
    if result:
        result.setdefault("source_tier", "canonical_locator")
        return result
    result = _canonical_stream_lookup(ami_id, region)
    if result:
        return result
    result = _authoritative_search_lookup(ami_id, region)
    if result:
        return result
    return _corroborated_search_lookup(ami_id, region)

def _q6_case_state(sequence_state, access_key):
    seed = json.dumps({
        "key": access_key,
        "validated_qids": sequence_state.get("validated_qids", []),
        "ts": time.time_ns(),
    }, sort_keys=True)
    return {"case_id": "q6-" + hashlib.sha256(seed.encode()).hexdigest()[:16]}


def _timeline_identity_events(identity_rows):
    out = []
    for raw in identity_rows:
        item = _normalise_identity_row(raw)
        out.append({
            "_time": item.get("event_time"),
            "eventTime": item.get("event_time"),
            "eventName": item.get("event_name"),
            "eventSource": item.get("event_source"),
            "accessKeyId": item.get("access_key"),
            "userName": item.get("iam_user"),
            "sourceIPAddress": item.get("source_ip"),
            "derived_access_key": item.get("derived_access_key"),
            "principalId": item.get("principal_id"),
            "arn": item.get("iam_arn"),
            "errorCode": item.get("error_code"),
        })
    return out


def _timeline_launch_events(launches):
    out = []
    for item in launches:
        out.append({
            "_time": item.get("event_time"),
            "eventTime": item.get("event_time"),
            "eventName": "RunInstances",
            "eventSource": "ec2.amazonaws.com",
            "eventID": item.get("event_id"),
            "accessKeyId": item.get("access_key"),
            "userName": item.get("iam_user"),
            "awsRegion": item.get("region"),
            "userAgent": item.get("user_agent"),
            "requestParameters": {"instancesSet": {"items": [{"imageId": item.get("image_id")}] }},
            "errorCode": item.get("error_code"),
        })
    return out


def _timeline_identity_from_relations(case_id, original_key, fallback):
    relations = timeline_related(original_key, case_id=case_id, limit=100)
    keys = {original_key}
    users = set()
    accepted_edges = []
    supporting_edges = []
    for rel in relations:
        confidence = float(rel.get("confidence_score") or 0.0)
        sufficient = bool(rel.get("sufficient_for_identity"))
        relation = str(rel.get("relation") or "")
        if sufficient and confidence >= 0.9 and relation in {"RELATED_CREDENTIAL", "USED_BY"}:
            accepted_edges.append(rel)
            for side in ("from", "to"):
                kind = str(rel.get(side + "_type") or "")
                value = str(rel.get(side) or "").strip()
                if kind == "access_key" and re.fullmatch(r"(?:AKIA|ASIA)[A-Z0-9]{16}", value):
                    keys.add(value)
                elif kind == "user" and value:
                    users.add(value)
        else:
            supporting_edges.append(rel)

    if fallback.get("iam_user"):
        users.add(fallback["iam_user"])
    evidenced_temp = sorted(k for k in keys if k != original_key and k.startswith("ASIA"))
    if len(users) > 1:
        return None, relations, "Timeline relationships map the compromised key to multiple IAM usernames."
    resolved = dict(fallback)
    resolved["iam_user"] = next(iter(users), fallback.get("iam_user", ""))
    resolved["derived_access_keys"] = evidenced_temp
    resolved["all_access_keys"] = [original_key] + evidenced_temp
    resolved["identity_relationships"] = accepted_edges
    resolved["supporting_relationships"] = supporting_edges
    return resolved, relations, None

def _timeline_first_launch(case_id, launches, identity):
    # FIRST semantics must operate on the complete persisted RunInstances
    # population.  timeline_map is a presentation view and may be limited, so use
    # an unbounded chronological query for selection and keep the map for diagnostics.
    all_rows = query_timeline(case_id=case_id, entity=None, limit=None, order="chronological_asc")
    launch_rows = [r for r in all_rows if r.get("event_type") == "RunInstances"]
    mapped = timeline_map(case_id=case_id, entity=None, limit=100)
    mapped["chronology_population_count"] = len(launch_rows)
    if not launch_rows:
        return [], mapped
    times = [parse_event_time(r.get("event_time")) for r in launch_rows]
    times = [t for t in times if t is not None]
    if not times:
        return [], mapped
    first_time = min(times)
    first_timeline_ids = {
        str(r.get("event_id") or "") for r in launch_rows
        if parse_event_time(r.get("event_time")) == first_time
    }
    selected = [x for x in launches if parse_event_time(x.get("event_time")) == first_time]
    mapped["first_event_time"] = first_time.isoformat().replace("+00:00", "Z")
    mapped["first_timeline_event_ids"] = sorted(x for x in first_timeline_ids if x)
    return selected, mapped


async def resolve_q6_from_sequence(session, sequence_state, metrics=None):
    metrics = metrics if metrics is not None else {}
    record = {"timestamp": time.time(), "kind": "q6_ubuntu_launch_resolution", "status": "started",
              "sequence_state": sequence_state, "searches": [], "stage_trace": []}

    def stop(stage, reason):
        record.update(status="blocked", stage=stage, reason=reason)
        metrics["final_validation_status"] = "q6_" + stage
        _trace_stage(record, stage, "blocked", reason=reason)
        _append_jsonl(record)
        return record

    access_key = extract_access_key(sequence_state)
    if not access_key:
        return stop("sequence_context", "No evidence-validated AWS access key is available in sequence state")
    metrics["q6_sequence_state_reuses"] = 1
    _trace_stage(record, "sequence_context", "complete", access_key_fact=True,
                 validated_qids=sequence_state.get("validated_qids", []))
    case_state = _q6_case_state(sequence_state, access_key)
    record["timeline_case_id"] = case_state["case_id"]

    identity_rows, error = await _splunk_search(session, _identity_query(access_key), record=record, metrics=metrics)
    if error:
        return stop("identity_retrieval", error)
    if len(identity_rows) >= 1001:
        return stop("identity_retrieval_limit", "Identity expansion reached its result limit")
    _trace_stage(record, "identity_retrieval", "complete", row_count=len(identity_rows))

    identity = _derive_identity_graph(identity_rows, access_key)
    record["identity_expansion_pre_timeline"] = identity
    if identity.get("status") != "resolved":
        return stop("identity_expansion", identity.get("reason") or "Could not resolve the compromised IAM identity")
    _trace_stage(record, "identity_graph", "complete", iam_user_count=len(identity.get("iam_users") or []),
                 derived_credential_count=len(identity.get("derived_access_keys") or []),
                 temporal_basis_events=identity.get("scope_basis_event_count"))

    identity_evidence = {"evidence_id": "Q6-IDENTITY", "source": "splunk", "tool": "search_oneshot",
                         "method_class": "timeline_identity_expansion", "query": _identity_query(access_key)}
    _, identity_timeline_rows = append_timeline_events(case_state, identity_evidence, _timeline_identity_events(identity_rows))
    metrics["q6_timeline_events"] = metrics.get("q6_timeline_events", 0) + len(identity_timeline_rows)
    identity, relations, timeline_error = _timeline_identity_from_relations(case_state["case_id"], access_key, identity)
    record["timeline_relationships"] = relations
    if timeline_error:
        return stop("timeline_identity", timeline_error)
    record["identity_expansion"] = identity
    metrics["q6_identity_expansions"] = 1
    metrics["q6_timeline_relationship_pivots"] = len(relations)
    metrics["q6_related_access_keys"] = len(identity.get("derived_access_keys") or [])
    _trace_stage(record, "timeline_relationships", "complete",
                 accepted_identity_edges=len(identity.get("identity_relationships") or []),
                 supporting_edges=len(identity.get("supporting_relationships") or []),
                 related_access_keys=len(identity.get("derived_access_keys") or []))

    anchor_candidates = [
        row for row in identity_timeline_rows
        if row.get("event_type") and any(
            rel.get("relation") == "RELATED_CREDENTIAL" and rel.get("event_id") == row.get("event_id")
            and rel.get("sufficient_for_identity")
            for rel in relations
        )
    ]
    if anchor_candidates:
        anchor_row = sorted(anchor_candidates, key=lambda r: parse_event_time(r.get("event_time")) or datetime.min.replace(tzinfo=timezone.utc))[0]
        record["timeline_anchor"] = set_anchor(
            case_state, anchor_row.get("event_time"), evidence_id=anchor_row.get("evidence_id"),
            reason="Evidence-derived credential relationship used as Q6 attack-phase anchor.", entity=access_key,
        )

    launches, raw_rows, selected_window, primary_mode, scope_history, scope_error = await _dynamic_launch_search(
        session, identity, record=record, metrics=metrics
    )
    record["temporal_scope_history"] = scope_history
    record["selected_temporal_window"] = selected_window
    identity["selected_temporal_window"] = selected_window
    metrics["q6_temporal_scope_expansions"] = len(scope_history)
    if scope_error:
        stage = "retrieval_limit" if any(token in scope_error.lower() for token in ("limit", "part of the counted")) else "temporal_scope"
        return stop(stage, scope_error)
    _trace_stage(record, "temporal_scope", "complete", route=primary_mode,
                 selected_window=selected_window, expansion_count=len(scope_history), candidate_count=len(launches or []))

    record["candidate_mode"] = primary_mode
    record["candidates"] = launches
    if not launches:
        return stop("no_launch_events", "No RunInstances events matched the evidence-derived IAM identity within the adaptively expanded attack scope")
    if any(x["epoch"] is None for x in launches):
        return stop("timestamp", "A candidate has a missing/invalid eventTime")
    if any(not x["event_id"] or not x["region"] for x in launches):
        return stop("identity", "A candidate lacks an event ID or region")

    launch_evidence = {"evidence_id": "Q6-LAUNCH", "source": "splunk", "tool": "search_oneshot",
                       "method_class": "chronological_first",
                       "query": _primary_query_for_identity(identity, selected_window) if primary_mode == "parsed_identity" else _raw_launch_query(identity, selected_window)}
    _, launch_timeline_rows = append_timeline_events(case_state, launch_evidence, _timeline_launch_events(launches))
    metrics["q6_timeline_events"] = metrics.get("q6_timeline_events", 0) + len(launch_timeline_rows)
    selected, timeline_snapshot = _timeline_first_launch(case_state["case_id"], launches, identity)
    record["timeline_map"] = timeline_snapshot
    if not selected:
        return stop("timeline_first", "Shared timeline could not establish the first RunInstances event")

    # Cross-check the shared timeline's FIRST against the complete in-memory
    # candidate population before accepting it.  This invariant is deliberately
    # independent of the presentation-limited timeline map.
    direct_signature = _earliest_signature(launches)
    timeline_signature = _earliest_signature(selected)
    if direct_signature != timeline_signature:
        metrics["q6_chronology_invariant_failures"] = metrics.get("q6_chronology_invariant_failures", 0) + 1
        review = _review_anomaly(record, metrics, "chronology_invariant_failure", {
            "candidate_count": len(launches),
            "timeline_population_count": timeline_snapshot.get("chronology_population_count"),
            "timeline_first_time": timeline_snapshot.get("first_event_time"),
            "direct_earliest_signature": direct_signature,
            "timeline_earliest_signature": timeline_signature,
        })
        # Python rejects the inconsistent selection and recomputes FIRST from the
        # complete candidate population.  Independent raw-boundary verification
        # below must still agree before release is possible.
        first_epoch = min(x["epoch"] for x in launches if x.get("epoch") is not None)
        selected = [x for x in launches if x.get("epoch") == first_epoch]
        record["chronology_recomputed_from_complete_candidates"] = True

    first = {x["event_id"]: x for x in selected}
    record["earliest_attempts"] = list(first.values())
    if any(not re.fullmatch(r"ami-(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{17})", x["image_id"]) for x in first.values()):
        return stop("ami_extract", "Earliest attempt has missing, ambiguous or invalid image ID; later attempts were not substituted")
    metrics["q6_first_event_resolutions"] = 1
    metrics["q6_timeline_first_selections"] = 1
    _trace_stage(record, "first_event", "complete", tied_first_count=len(first),
                 first_event_ids=sorted(first), first_time=min(x["event_time"] for x in first.values()))

    if case_state.get("anchor"):
        minutes = float((selected_window or {}).get("minutes") or 30)
        record["timeline_after_anchor"] = timeline_after(
            case_state["case_id"], minutes=minutes, entity=identity.get("iam_user") or access_key, limit=100
        )

    # Independent chronology validation always uses a raw boundary reduction.
    # It does not reuse the parsed/spath route that may already have been declared
    # unhealthy, and it differs materially from the primary Python-min method.
    verify_rows, error = await _splunk_search(
        session, _raw_boundary_verification_query(identity, selected_window), record=record, metrics=metrics
    )
    if len(verify_rows) >= 1001:
        return stop("retrieval_limit", "Raw boundary verification reached its result limit")
    verification = [_normalise_launch_row(x) for x in verify_rows]
    verification = [x for x in verification if x.get("event_source") == "ec2.amazonaws.com"
                    and x.get("event_name") == "RunInstances" and _row_matches_identity(x, identity)]
    if any(x["epoch"] is None for x in verification):
        return stop("timestamp", "Raw boundary verification contains an invalid timestamp")
    record["verification"] = verification
    record["verification_method"] = "raw_splunk_min_time_boundary_then_python_identity_validation"
    fields = ("event_id", "epoch", "region", "image_id", "iam_user", "access_key")
    signatures = lambda xs: {tuple(x.get(k) for k in fields) for x in xs}
    if error or signatures(verification) != signatures(first.values()):
        metrics["q6_independent_validation_conflicts"] = metrics.get("q6_independent_validation_conflicts", 0) + 1
        review = _review_anomaly(record, metrics, "independent_validation_conflict", {
            "primary_mode": primary_mode,
            "selected_window": selected_window,
            "primary_signature": sorted(map(str, signatures(first.values()))),
            "verification_signature": sorted(map(str, signatures(verification))),
            "verification_error": error,
            "verification_row_count": len(verify_rows),
        })
        return stop("chronology_verification", error or "Independent earliest-event set differs (identity/time/region/AMI)")
    metrics["q6_first_event_verifications"] = 1
    _trace_stage(record, "chronology_verification", "complete", verified_event_count=len(verification))

    enrichments = []
    for item in first.values():
        metrics["q6_enrichment_calls"] = metrics.get("q6_enrichment_calls", 0) + 1
        metrics["external_calls"] = metrics.get("external_calls", 0) + 1
        print(f"Q6 ENRICH AMI: {item['image_id']} / {item['region']}")
        enriched = resolve_ubuntu_ami(item["image_id"], item["region"])
        if not enriched:
            record["partial_enrichments"] = enrichments
            review = _review_anomaly(record, metrics, "external_enrichment_failure", {
                "stage": "ami_release_enrichment",
                "ami_present": bool(item.get("image_id")),
                "region_present": bool(item.get("region")),
                "attempted_hierarchy": ["canonical_locator", "canonical_stream", "ubuntu_authoritative_search", "two_domain_corroboration"],
            })
            return stop("external_enrichment", "Exact AMI + region could not be connected to one Ubuntu release through the enrichment hierarchy")
        enrichments.append(enriched)
    release_keys = {(e["release"].get("version", ""), e["release"].get("series", "")) for e in enrichments}
    if len(release_keys) != 1:
        return stop("ambiguous_first", "Tied earliest events resolve to different Ubuntu releases")
    _trace_stage(record, "ami_release_enrichment", "complete",
                 source_tiers=[e.get("source_tier") for e in enrichments], release_keys=sorted(release_keys))

    release = dict(enrichments[0]["release"])
    metrics["q6_codename_enrichment_calls"] = metrics.get("q6_codename_enrichment_calls", 0) + 1
    metrics["external_calls"] = metrics.get("external_calls", 0) + 1
    print(f"Q6 ENRICH RELEASE: version={release.get('version','')} series={release.get('series','')}")
    codename_evidence = resolve_ubuntu_codename(release)
    if not codename_evidence:
        review = _review_anomaly(record, metrics, "external_enrichment_failure", {
            "stage": "codename_enrichment",
            "release_version_present": bool(release.get("version")),
            "release_series_present": bool(release.get("series")),
            "required_source": "official Ubuntu release metadata",
            "direct_official_urls": _official_codename_urls(release.get("version", "")),
            "fallback_policy": "official Ubuntu search only after deterministic official version pages",
        })
        return stop("codename_enrichment", "The evidenced Ubuntu release could not be connected to one official two-word codename")
    answer = str(codename_evidence.get("codename") or "").strip()
    if codename_evidence.get("source_tier") == "official_direct_release_page":
        metrics["q6_codename_direct_resolutions"] = metrics.get("q6_codename_direct_resolutions", 0) + 1
    else:
        metrics["q6_codename_search_fallback_resolutions"] = metrics.get("q6_codename_search_fallback_resolutions", 0) + 1
    _trace_stage(
        record, "codename_enrichment", "complete",
        source=codename_evidence.get("source"), url=codename_evidence.get("url"),
        source_tier=codename_evidence.get("source_tier"),
        corroborating_source_count=len(codename_evidence.get("corroborating_sources") or []),
    )
    if len(answer.split()) != 2:
        return stop("answer_contract", "Externally evidenced codename is not exactly two words")
    _trace_stage(record, "answer_contract", "complete", word_count=2)

    primary = next(iter(first.values()))
    record.update(status="validated", access_key=access_key, resolved_iam_user=identity.get("iam_user"),
                  derived_access_keys=identity.get("derived_access_keys") or [], first_attempt=primary,
                  enrichment=enrichments[0], enrichments=enrichments, codename_enrichment=codename_evidence,
                  answer=answer, tied_earliest_count=len(first),
                  user_agent_consistent_with_q2=bool(sequence_state.get("adversary_user_agent") and primary["user_agent"] == sequence_state["adversary_user_agent"]))
    metrics.update(q6_enrichment_resolutions=len(enrichments), q6_codename_enrichment_resolutions=1,
                   canonical_release_allows=1, final_validation_status="validated")
    _trace_stage(record, "release", "validated", source_tier=enrichments[0].get("source_tier"))
    _append_jsonl(record)
    return record

