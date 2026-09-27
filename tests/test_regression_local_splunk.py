import json

import pytest

from commander_agent.core.preflight import splunk_search_preflight
from commander_agent.core.environment import make_environment


class TextPart:
    def __init__(self, text):
        self.text = text


class ToolResult:
    def __init__(self, text):
        self.content = [TextPart(text)]


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return ToolResult(json.dumps(self.payload))


@pytest.mark.asyncio
async def test_splunk_preflight_passes_and_uses_historical_window():
    s = FakeSession({
        "event_count": 1,
        "events": [{"botsv3_count": "2030269"}],
    })
    ok, info = await splunk_search_preflight(s)
    assert ok is True
    assert info["kind"] == "ok"
    assert s.calls[0][0] == "search_oneshot"
    args = s.calls[0][1]
    assert args["earliest_time"] == "0"
    assert args["latest_time"] == "now"


@pytest.mark.asyncio
async def test_splunk_preflight_stops_on_unauthorized():
    s = FakeSession({
        "error": "Search failed",
        "details": {
            "error": json.dumps({"messages": [{"type": "ERROR", "text": "Unauthorized"}]})
        },
    })
    ok, info = await splunk_search_preflight(s)
    assert ok is False
    assert info["kind"] == "authorization_failure"
    assert info["fatal"] is True


def test_environment_runtime_password_targets_local_splunk(monkeypatch):
    env = make_environment(splunk_username="example-user", splunk_password="example-pass")
    assert env["SPLUNK_HOST"] == "127.0.0.1"
    assert env["SPLUNK_PORT"] == "8089"
    assert env["SPLUNK_USERNAME"] == "example-user"
    assert env["SPLUNK_PASSWORD"] == "example-pass"
    assert env["VERIFY_SSL"] == "false"
