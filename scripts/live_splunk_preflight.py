from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Running this file directly makes Python put scripts/ rather than the project root
# on sys.path. Add the package root explicitly before importing commander_agent.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from commander_agent.config import (
    MCP_CWD,
    MCP_PYTHON,
    MCP_SERVER,
    SPLUNK_PASSWORD,
    SPLUNK_USERNAME,
)
from commander_agent.core.environment import make_environment
from commander_agent.core.preflight import local_preflight, splunk_search_preflight


async def _run():
    ok, problems = local_preflight()
    if not ok:
        for problem in problems:
            print("Local preflight failed: " + problem)
        return 1

    username = (SPLUNK_USERNAME or "").strip()
    password = SPLUNK_PASSWORD or ""
    if not username or not password:
        print("Live Splunk preflight skipped: credentials are missing.")
        return 2

    server = StdioServerParameters(
        command=MCP_PYTHON,
        args=[MCP_SERVER],
        cwd=MCP_CWD,
        env=make_environment(splunk_username=username, splunk_password=password),
    )
    try:
        async with stdio_client(server, errlog=sys.stderr) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                ok, info = await splunk_search_preflight(session)
    except BaseException as exc:
        print(f"Live Splunk preflight failed: {type(exc).__name__}: {exc}")
        return 1

    if not ok:
        print("Live Splunk preflight failed.")
        print("Type   : " + str(info.get("kind", "unknown")))
        print("Reason : " + str(info.get("message", "unknown failure")))
        return 1

    count = info.get("botsv3_count")
    suffix = f" count={count}" if count is not None else ""
    print("Live Splunk preflight: PASS - BOTSv3 searchable." + suffix)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
