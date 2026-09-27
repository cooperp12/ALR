from __future__ import annotations

import json
import re
from dataclasses import dataclass


_TRANSFORMING_COMMANDS = {
    "stats", "chart", "timechart", "eventstats", "streamstats", "top", "rare",
    "transaction", "table", "fields", "dedup", "sort", "head", "tail",
}


def split_spl_pipeline(query: str) -> list[str]:
    """Split SPL on pipeline separators while respecting quoted regex/string pipes."""
    text = str(query or "")
    parts: list[str] = []
    buf: list[str] = []
    quote = None
    escape = False
    for ch in text:
        if escape:
            buf.append(ch)
            escape = False
            continue
        if ch == "\\":
            buf.append(ch)
            escape = True
            continue
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in {"'", '"'}:
            quote = ch
            buf.append(ch)
            continue
        if ch == "|":
            part = "".join(buf).strip()
            if part:
                parts.append(part)
            buf = []
            continue
        buf.append(ch)
    part = "".join(buf).strip()
    if part:
        parts.append(part)
    return parts


def _command_name(stage: str) -> str:
    m = re.match(r"\s*([A-Za-z_][\w-]*)", stage or "")
    return m.group(1).lower() if m else ""


def scope_prefix_stages(query: str) -> list[str]:
    stages = split_spl_pipeline(query)
    out: list[str] = []
    for i, stage in enumerate(stages):
        if i > 0 and _command_name(stage) in _TRANSFORMING_COMMANDS:
            break
        out.append(stage.strip())
    return out


def _norm(text: str) -> str:
    value = re.sub(r"\s+", " ", str(text or "").strip())
    value = re.sub(r"\s*=\s*", "=", value)
    return value.lower()


def _head_atoms(stage: str) -> list[str]:
    # Extract field=value terms mechanically. This is syntax preservation, not
    # domain interpretation. Quoted values are retained.
    pattern = re.compile(
        r"(?<![\w.])([A-Za-z_][\w.:-]*)\s*=\s*(\"(?:\\.|[^\"])*\"|'(?:\\.|[^'])*'|[^\s]+)"
    )
    atoms = []
    for key, value in pattern.findall(stage or ""):
        atom = _norm(f"{key}={value}")
        if atom not in atoms:
            atoms.append(atom)
    return atoms


def build_scope_contract(primary_query: str, check_query: str = "") -> dict:
    """Derive immutable query-scope syntax from the strategy itself.

    No question/domain values are embedded here. The contract is mechanically
    extracted from the reducing queries selected for the current investigation.
    """
    p = scope_prefix_stages(primary_query)
    c = scope_prefix_stages(check_query) if check_query else []
    common: list[str] = []
    if c:
        for a, b in zip(p, c):
            if _norm(a) != _norm(b):
                break
            common.append(a)
    else:
        common = list(p)

    if not common:
        common = list(p)

    head = common[0] if common else ""
    required_head_atoms = _head_atoms(head)
    required_filter_stages = [_norm(x) for x in common[1:]]
    return {
        "version": 1,
        "base_scope_spl": " | ".join(common).strip(),
        "required_head_atoms": required_head_atoms,
        "required_filter_stages": required_filter_stages,
        "source_primary": primary_query or "",
        "source_check": check_query or "",
    }


def scope_violations(query: str, contract: dict | None) -> list[str]:
    contract = contract or {}
    if not contract.get("base_scope_spl"):
        return []
    stages = scope_prefix_stages(query)
    if not stages:
        return ["proposed query has no searchable scope prefix"]

    proposed_atoms = set(_head_atoms(stages[0]))
    errors = []
    for atom in contract.get("required_head_atoms", []):
        if atom not in proposed_atoms:
            errors.append(f"required search-scope term removed or changed: {atom}")

    proposed_filters = {_norm(x) for x in stages[1:]}
    for required in contract.get("required_filter_stages", []):
        preserved = required in proposed_filters or any(
            required in proposed for proposed in proposed_filters
        )
        if not preserved:
            errors.append(f"required filter stage removed or changed: {required}")
    return errors


def render_scope_contract(contract: dict | None) -> str:
    if not contract:
        return "[none]"
    return json.dumps(contract, ensure_ascii=False, indent=2)


def detectable_scope_change(query: str, contract: dict | None) -> bool:
    """Return whether the searchable scope prefix materially differs from the contract.

    This is syntax-level only. It does not decide whether a requested scope change is
    semantically justified; the scope guard still owns that review. It exists to catch
    action/query mismatches such as declaring an expansion while reusing the identical
    scoped search.
    """
    contract = contract or {}
    base = str(contract.get("base_scope_spl") or "").strip()
    if not base:
        return False
    proposed = " | ".join(scope_prefix_stages(query)).strip()
    return bool(proposed and _norm(proposed) != _norm(base))
