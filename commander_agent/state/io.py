import hashlib
import json
import os
from pathlib import Path

from commander_agent.skills.structured_output_validation.scripts.schema import strict_json_roundtrip


def json_load_file(path, default):
    try:
        p = Path(path)
        if not p.exists():
            return default
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _validated_json(value):
    ok, canonical, error = strict_json_roundtrip(value)
    if not ok:
        raise ValueError(f"Object is not valid JSON state: {error}")
    return canonical


def json_write_atomic(path, value):
    canonical = _validated_json(value)
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(
        json.dumps(canonical, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    # Prove the exact bytes we are about to promote parse as JSON.
    json.loads(tmp.read_text(encoding="utf-8"))
    os.replace(tmp, p)


def append_jsonl(path, record):
    try:
        canonical = _validated_json(record)
        line = json.dumps(canonical, ensure_ascii=False)
        # Validate line-by-line before append so one malformed record cannot poison
        # the append-only ledger.
        json.loads(line)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.write("\n")
        return True
    except Exception as exc:
        print(f"[Ledger warning: {type(exc).__name__}: {exc}]")
        return False


def stable_hash(value):
    canonical = _validated_json(value)
    encoded = json.dumps(
        canonical,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
