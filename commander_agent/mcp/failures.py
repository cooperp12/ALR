"""Deterministic classification of Splunk/MCP failures.

This module is intentionally model-free. Infrastructure failures should not consume
LLM rounds. In particular, authentication/authorization failures are non-retryable
until credentials or permissions change.
"""

from __future__ import annotations

import json
import re
from typing import Any

AUTH_PATTERNS = (
    r"\bunauthori[sz]ed\b",
    r"\bforbidden\b",
    r"\bpermission denied\b",
    r"\baccess denied\b",
    r"\bauthentication failed\b",
    r"\binvalid credentials?\b",
    r"\bnot authenticated\b",
    r"\bhttp\s*401\b",
    r"\bhttp\s*403\b",
    r"\bstatus(?:\s*code)?\s*[:=]?\s*401\b",
    r"\bstatus(?:\s*code)?\s*[:=]?\s*403\b",
)

CONNECTION_PATTERNS = (
    r"connection refused",
    r"connection reset",
    r"failed to connect",
    r"name or service not known",
    r"getaddrinfo failed",
    r"timed? out",
    r"timeout",
)

INDEX_PATTERNS = (
    r"unknown index",
    r"index .* does not exist",
    r"cannot find index",
    r"no such index",
)


def _collect_strings(value: Any, out: list[str], depth: int = 0) -> None:
    if depth > 8 or value is None:
        return
    if isinstance(value, str):
        text = value.strip()
        if text:
            out.append(text)
            # Splunk MCP frequently nests JSON as a string in details.error.
            if text[:1] in "[{":
                try:
                    nested = json.loads(text)
                except Exception:
                    nested = None
                if nested is not None:
                    _collect_strings(nested, out, depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in {"error", "errors", "detail", "details", "message", "messages", "text", "reason"}:
                _collect_strings(item, out, depth + 1)
            elif isinstance(item, (dict, list, tuple)):
                _collect_strings(item, out, depth + 1)
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            _collect_strings(item, out, depth + 1)


def error_text(payload_or_text: Any) -> str:
    strings: list[str] = []
    if isinstance(payload_or_text, str):
        raw = payload_or_text.strip()
        if raw:
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = raw
            _collect_strings(parsed, strings)
    else:
        _collect_strings(payload_or_text, strings)
    # Preserve order while deduplicating.
    seen = set()
    unique = []
    for item in strings:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return " | ".join(unique)


def classify_splunk_failure(payload_or_text: Any) -> dict:
    text = error_text(payload_or_text)
    low = text.lower()

    def any_match(patterns):
        return any(re.search(p, low, flags=re.IGNORECASE) for p in patterns)

    if any_match(AUTH_PATTERNS):
        return {
            "kind": "authorization_failure",
            "code": "UNAUTHORIZED",
            "retryable": False,
            "fatal": True,
            "layer": "splunk",
            "message": text or "Splunk authentication/authorization failed.",
        }
    if any_match(CONNECTION_PATTERNS):
        return {
            "kind": "connection_failure",
            "code": "CONNECTION_FAILED",
            "retryable": True,
            "fatal": False,
            "layer": "splunk",
            "message": text or "Splunk connection failed.",
        }
    if any_match(INDEX_PATTERNS):
        return {
            "kind": "dataset_unavailable",
            "code": "BOTSV3_INDEX_UNAVAILABLE",
            "retryable": False,
            "fatal": True,
            "layer": "splunk",
            "message": text or "BOTSv3 index is unavailable.",
        }
    return {
        "kind": "tool_error",
        "code": "SPLUNK_TOOL_ERROR",
        "retryable": False,
        "fatal": False,
        "layer": "splunk",
        "message": text or "Splunk tool returned an error.",
    }
