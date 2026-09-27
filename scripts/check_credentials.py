from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CREDENTIALS = ROOT / "config" / "credentials.json"


def load_credentials():
    data = {}
    if CREDENTIALS.exists():
        try:
            parsed = json.loads(CREDENTIALS.read_text(encoding="utf-8-sig"))
            if isinstance(parsed, dict):
                data = parsed
        except Exception as exc:
            print(f"Credentials file is not valid JSON: {type(exc).__name__}: {exc}")
            return "", "", "invalid"

    username = (os.getenv("SPLUNK_USERNAME") or str(data.get("splunk_username", ""))).strip()
    password = os.getenv("SPLUNK_PASSWORD") or str(data.get("splunk_password", ""))
    source = "environment" if os.getenv("SPLUNK_USERNAME") and os.getenv("SPLUNK_PASSWORD") else "config/credentials.json"
    return username, password, source


def main():
    username, password, source = load_credentials()
    if username and password:
        print(f"Splunk credentials: present ({source}; values not displayed)")
        return 0
    if source == "invalid":
        return 2
    print(f"Splunk credentials: missing or incomplete in {CREDENTIALS}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
