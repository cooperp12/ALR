"""Minimal OpenAI Responses API client using only the Python standard library.

The API key is read at runtime from the configured key file and is never written
into ALR legacy compatibility state/logs.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from commander_agent.config import (
    OPENAI_KEY_FILE,
    OPENAI_RESPONSES_URL,
    OPENAI_TIMEOUT_SECONDS,
)


def read_api_key():
    try:
        value = OPENAI_KEY_FILE.read_text(encoding="utf-8-sig").strip()
    except Exception as exc:
        return None, f"key file unavailable: {type(exc).__name__}: {exc}"
    if not value:
        return None, "key file is empty"
    # Support a raw key as requested; tolerate KEY=value without logging the value.
    if "=" in value and value.split("=", 1)[0].strip().upper() in {"OPENAI_API_KEY", "API_KEY"}:
        value = value.split("=", 1)[1].strip().strip('"').strip("'")
    if not value:
        return None, "key file did not contain a usable key"
    return value, ""


def _extract_output_text(payload):
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]
    parts = []
    for item in payload.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []) or []:
            if not isinstance(content, dict):
                continue
            if content.get("type") in {"output_text", "text"} and isinstance(content.get("text"), str):
                parts.append(content["text"])
    return "\n".join(parts).strip()


def create_response(api_key, model, prompt, effort="low", max_output_tokens=1200):
    body = {
        "model": model,
        "input": prompt,
        "reasoning": {"effort": effort},
        "max_output_tokens": int(max_output_tokens),
    }
    request = urllib.request.Request(
        OPENAI_RESPONSES_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "BOTSv3-ALR legacy compatibility/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=OPENAI_TIMEOUT_SECONDS) as response:
            raw = response.read(2_000_000).decode("utf-8", errors="replace")
            payload = json.loads(raw)
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read(4096).decode("utf-8", errors="replace")
        except Exception:
            detail = str(exc)
        # Never include headers/request objects because they can contain auth material.
        raise RuntimeError(f"OpenAI HTTP {exc.code}: {detail[:1200]}") from None
    except Exception as exc:
        raise RuntimeError(f"OpenAI request failed: {type(exc).__name__}: {exc}") from None

    text = _extract_output_text(payload)
    usage = payload.get("usage") if isinstance(payload, dict) else None
    return {
        "text": text,
        "model": payload.get("model", model) if isinstance(payload, dict) else model,
        "usage": usage if isinstance(usage, dict) else {},
        "response_id": payload.get("id") if isinstance(payload, dict) else None,
    }
