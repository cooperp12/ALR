from __future__ import annotations

import re
import sys
from pathlib import Path


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: patch_splunk_mcp.py <server.py>")
    path = Path(sys.argv[1]).resolve()
    if not path.exists():
        raise SystemExit(f"server.py not found: {path}")

    text = path.read_text(encoding="utf-8-sig")
    original = text

    # The current Splunk MCP server uses FastMCP's former `description=` keyword.
    # MCP 1.30.0 expects the equivalent constructor keyword `instructions=`.
    if "FastMCP(" in text:
        text, count = re.subn(
            r'description\s*=\s*os\.getenv\("SERVER_DESCRIPTION"',
            r'instructions=os.getenv("SERVER_DESCRIPTION"',
            text,
            count=1,
        )
    else:
        count = 0

    if text != original:
        backup = path.with_suffix(path.suffix + ".ALR.bak")
        if not backup.exists():
            backup.write_text(original, encoding="utf-8")
        path.write_text(text, encoding="utf-8")
        print("Applied FastMCP description->instructions compatibility patch.")
    elif 'instructions=os.getenv("SERVER_DESCRIPTION"' in text:
        print("FastMCP compatibility patch already present.")
    else:
        print("No FastMCP description compatibility patch was required.")


if __name__ == "__main__":
    main()
