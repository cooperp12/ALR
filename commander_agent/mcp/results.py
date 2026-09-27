import json
import re
from commander_agent.config import MAX_EVENT_CHARS,MAX_EVENTS_FOR_MODEL,MAX_TOTAL_MODEL_RESULT_CHARS
def clip_text(value, limit):
    if not isinstance(value, str):
        return value

    if len(value) <= limit:
        return value

    half = max(1, (limit - 60) // 2)

    return (
        value[:half]
        + "...[TRUNCATED]..."
        + value[-half:]
    )

def compact_nested(value, max_chars=800):
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            default=str,
        )
    except Exception:
        text = str(value)

    return clip_text(text, max_chars)

def parse_raw_json(raw):
    if not isinstance(raw, str):
        return None

    raw = raw.strip()

    if not raw:
        return None

    try:
        parsed = json.loads(raw)
    except Exception:
        return None

    if isinstance(parsed, dict):
        return parsed

    return None

def extract_email_metadata(text):
    if not isinstance(text, str):
        return {}

    result = {}

    header_patterns = {
        "subject": r"(?im)^Subject:\s*(.+)$",
        "from": r"(?im)^From:\s*(.+)$",
        "to": r"(?im)^To:\s*(.+)$",
    }

    for key, pattern in header_patterns.items():
        match = re.search(pattern, text)

        if match:
            result[key] = match.group(1).strip()

    urls = re.findall(
        r'https?://[^\s<>"\']+',
        text,
        flags=re.IGNORECASE,
    )

    unique_urls = []

    for url in urls:
        url = url.rstrip(".,);]")

        if url not in unique_urls:
            unique_urls.append(url)

    if unique_urls:
        result["urls"] = unique_urls[:12]

    return result

def compact_cloudtrail_event(event, raw_json):
    identity = raw_json.get("userIdentity") or {}

    compact = {
        "_time": event.get("_time") or raw_json.get("eventTime"),
        "sourcetype": (
            event.get("sourcetype")
            or event.get("_sourcetype")
            or "aws:cloudtrail"
        ),
        "eventTime": raw_json.get("eventTime"),
        "eventName": raw_json.get("eventName"),
        "eventSource": raw_json.get("eventSource"),
        "errorCode": raw_json.get("errorCode"),
        "errorMessage": raw_json.get("errorMessage"),
        "userAgent": raw_json.get("userAgent"),
        "sourceIPAddress": raw_json.get("sourceIPAddress"),
        "accessKeyId": identity.get("accessKeyId"),
        "userName": identity.get("userName"),
        "awsRegion": raw_json.get("awsRegion"),
    }

    if raw_json.get("requestParameters") is not None:
        compact["requestParameters"] = compact_nested(
            raw_json.get("requestParameters"),
            max_chars=1000,
        )

    if raw_json.get("responseElements") is not None:
        compact["responseElements"] = compact_nested(
            raw_json.get("responseElements"),
            max_chars=800,
        )

    return {
        key: value
        for key, value in compact.items()
        if value is not None
    }

def compact_json_event(event, raw_json):
    text_parts = []

    for key in ("content", "body", "message"):
        value = raw_json.get(key)

        if isinstance(value, list):
            text_parts.extend(str(item) for item in value[:30])

        elif isinstance(value, str):
            text_parts.append(value)

    combined = "\n".join(text_parts)
    email_meta = extract_email_metadata(combined)

    for src, dst in (
        ("Subject", "subject"),
        ("SenderAddress", "from"),
        ("RecipientAddress", "to"),
        ("MessageId", "message_id"),
        ("Status", "status"),
    ):
        if src in raw_json and dst not in email_meta:
            email_meta[dst] = raw_json[src]

    if email_meta:
        compact = {
            "_time": event.get("_time"),
            "sourcetype": event.get("sourcetype") or event.get("_sourcetype"),
            **email_meta,
        }

        if combined:
            compact["content_excerpt"] = clip_text(combined, 1600)

        return compact

    return {
        "_time": event.get("_time"),
        "sourcetype": event.get("sourcetype") or event.get("_sourcetype"),
        "json": compact_nested(raw_json, max_chars=1200),
    }

def compact_generic_event(event):
    drop_fields = {
        "_bkt",
        "_cd",
        "_indextime",
        "_serial",
        "_si",
        "_subsecond",
        "linecount",
        "splunk_server",
        "index",
    }

    compact = {}

    for key, value in event.items():
        if key in drop_fields or key == "_raw":
            continue

        if isinstance(value, (dict, list)):
            compact[key] = compact_nested(value, max_chars=700)

        elif isinstance(value, str):
            compact[key] = clip_text(value, 700)

        else:
            compact[key] = value

    if event.get("_raw"):
        compact["_raw_excerpt"] = clip_text(event["_raw"], 1000)

    return compact

def normalise_event(event):
    if not isinstance(event, dict):
        return {
            "value": clip_text(str(event), MAX_EVENT_CHARS)
        }

    raw_json = parse_raw_json(event.get("_raw"))

    sourcetype = (
        event.get("sourcetype")
        or event.get("_sourcetype")
        or ""
    )

    if raw_json:
        if (
            "cloudtrail" in sourcetype.lower()
            or raw_json.get("eventName")
            or raw_json.get("eventSource")
        ):
            compact = compact_cloudtrail_event(event, raw_json)
        else:
            compact = compact_json_event(event, raw_json)
    else:
        compact = compact_generic_event(event)

    serialized = json.dumps(
        compact,
        ensure_ascii=False,
        default=str,
    )

    if len(serialized) > MAX_EVENT_CHARS:
        for key in list(compact.keys()):
            value = compact[key]

            if isinstance(value, str) and len(value) > 350:
                compact[key] = clip_text(value, 350)

    return compact

def compact_search_result(result_text):
    try:
        data = json.loads(result_text)
    except Exception:
        return clip_text(result_text, MAX_TOTAL_MODEL_RESULT_CHARS)

    if not isinstance(data, dict):
        return clip_text(result_text, MAX_TOTAL_MODEL_RESULT_CHARS)

    events = data.get("events")

    if events is None:
        events = data.get("results")

    if not isinstance(events, list):
        return clip_text(
            json.dumps(
                data,
                indent=2,
                ensure_ascii=False,
                default=str,
            ),
            MAX_TOTAL_MODEL_RESULT_CHARS,
        )

    normalised = [
        normalise_event(event)
        for event in events[:MAX_EVENTS_FOR_MODEL]
    ]

    compact = {
        "query": data.get("query"),
        "event_count": data.get("event_count", len(events)),
        "rows_returned_to_model": len(normalised),
        "rows_omitted": max(0, len(events) - len(normalised)),
        "events": normalised,
        "search_params": data.get("search_params"),
    }

    text = json.dumps(
        compact,
        indent=2,
        ensure_ascii=False,
        default=str,
    )

    if len(text) <= MAX_TOTAL_MODEL_RESULT_CHARS:
        return text

    reduced = []

    for event in normalised:
        trial = dict(compact)
        trial["events"] = reduced + [event]
        trial["rows_returned_to_model"] = len(reduced) + 1
        trial["rows_omitted"] = len(events) - len(reduced) - 1

        trial_text = json.dumps(
            trial,
            indent=2,
            ensure_ascii=False,
            default=str,
        )

        if len(trial_text) > MAX_TOTAL_MODEL_RESULT_CHARS:
            break

        reduced.append(event)

    compact["events"] = reduced
    compact["rows_returned_to_model"] = len(reduced)
    compact["rows_omitted"] = max(0, len(events) - len(reduced))

    return json.dumps(
        compact,
        indent=2,
        ensure_ascii=False,
        default=str,
    )

