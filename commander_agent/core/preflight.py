from __future__ import annotations

import json
from pathlib import Path

from commander_agent.config import (
    INVESTIGATOR_MODEL,
    SUPERVISOR_MODEL,
    MCP_PYTHON,
    MCP_SERVER,
    MCP_CWD,
    SPLUNK_PREFLIGHT_QUERY,
    BOTSV3_EARLIEST,
    BOTSV3_LATEST,
)
from commander_agent.skills.structured_output_validation.scripts.schema import (
    validate_search_result_text,
)


def _tool_result_to_text(result):
    parts = []
    for item in getattr(result, "content", []) or []:
        if hasattr(item, "text"):
            parts.append(item.text)
        else:
            parts.append(str(item))
    return "\n".join(parts) if parts else str(result)


def ollama_preflight():
    try:
        import ollama
        ollama.show(INVESTIGATOR_MODEL)
        ollama.show(SUPERVISOR_MODEL)
        return True, (
            "Ollama reachable; role models available: "
            f"Investigator={INVESTIGATOR_MODEL}, Supervisor={SUPERVISOR_MODEL}."
        )
    except Exception as exc:
        return False, f"Ollama role-model preflight failed: {type(exc).__name__}: {exc}"


def local_preflight():
    problems = []
    for label, path in (
        ("MCP Python", MCP_PYTHON),
        ("MCP server", MCP_SERVER),
        ("MCP cwd", MCP_CWD),
    ):
        if not Path(path).exists():
            problems.append(f"{label} not found: {path}")
    return (not problems, problems)


async def splunk_search_preflight(session):
    """Prove that MCP can authenticate to Splunk and search the BOTSv3 index.

    MCP session initialisation alone only proves the subprocess is reachable. ALR legacy compatibility
    does not ask either LLM to investigate until this real search succeeds.
    """
    args = {
        "query": SPLUNK_PREFLIGHT_QUERY,
        "earliest_time": BOTSV3_EARLIEST,
        "latest_time": BOTSV3_LATEST,
        "max_count": 5,
        "output_format": "json",
    }
    try:
        result = await session.call_tool("search_oneshot", arguments=args)
        result_text = _tool_result_to_text(result)
    except Exception as exc:
        return False, {
            "kind": "mcp_execution_failure",
            "fatal": True,
            "message": f"MCP search execution failed: {type(exc).__name__}: {exc}",
            "query": SPLUNK_PREFLIGHT_QUERY,
        }

    validation = validate_search_result_text(result_text)
    if not validation.get("ok"):
        failure = validation.get("failure") or {
            "kind": validation.get("kind", "invalid_result"),
            "fatal": True,
            "message": "; ".join(validation.get("errors") or []) or "Invalid Splunk search result.",
        }
        return False, {
            **failure,
            "query": SPLUNK_PREFLIGHT_QUERY,
        }

    payload = validation.get("payload") or {}
    rows = payload.get("events", payload.get("results"))
    count_value = None
    if isinstance(rows, list) and rows:
        row = rows[0] if isinstance(rows[0], dict) else {}
        for key in ("botsv3_count", "count"):
            if key in row:
                count_value = row.get(key)
                break

    if count_value is not None:
        try:
            numeric_count = int(float(str(count_value).replace(",", "")))
        except Exception:
            numeric_count = None
        if numeric_count == 0:
            return False, {
                "kind": "dataset_empty",
                "code": "BOTSV3_EMPTY",
                "retryable": False,
                "fatal": True,
                "message": "Splunk search succeeded but index=botsv3 contains zero events.",
                "botsv3_count": 0,
                "query": SPLUNK_PREFLIGHT_QUERY,
            }
        if numeric_count is not None:
            count_value = numeric_count

    return True, {
        "kind": "ok",
        "fatal": False,
        "message": "Splunk authentication/search preflight passed; BOTSv3 is searchable.",
        "botsv3_count": count_value,
        "query": SPLUNK_PREFLIGHT_QUERY,
    }
