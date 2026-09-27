from __future__ import annotations

import json
from pathlib import Path

from commander_agent.reasoning.local_ollama import structured_chat, message_content
from commander_agent.skills.structured_output_validation.scripts.schema import parse_model_object
from commander_agent.state.scope import render_scope_contract


SCOPE_REVIEW_SCHEMA = {
    "type": "object",
    "required": ["decision", "reason", "changed_constraints", "preserved_constraints"],
    "properties": {
        "decision": {"type": "string", "enum": ["ALLOW", "DENY"]},
        "reason": {"type": "string"},
        "changed_constraints": {"type": "array", "items": {"type": "string"}},
        "preserved_constraints": {"type": "array", "items": {"type": "string"}},
    },
}


def _skill_text():
    path = Path(__file__).resolve().parents[1] / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            text = parts[2]
    return text.strip()


def review_scope_change(question, scope_contract, proposed_query, justification, metrics=None):
    prompt = f"""
You are executing the SCOPE INVARIANT GUARD skill below.
Return only JSON matching the supplied schema.
Do not solve the investigation and do not use any expected answer.

=== SKILL ===
{_skill_text()}

=== QUESTION ===
{question}

=== CURRENT SCOPE CONTRACT ===
{render_scope_contract(scope_contract)}

=== PROPOSED QUERY ===
{proposed_query}

=== EXPLICIT JUSTIFICATION ===
{justification or '[none]'}
""".strip()
    try:
        response = structured_chat(
            role="supervisor",
            messages=[{"role": "user", "content": prompt}],
            schema=SCOPE_REVIEW_SCHEMA,
            metrics=metrics,
            purpose="scope_invariant_guard",
            think=False,
        )
        if metrics is not None:
            metrics["scope_guard_reviews"] = metrics.get("scope_guard_reviews", 0) + 1
        ok, obj, errors = parse_model_object(message_content(response), SCOPE_REVIEW_SCHEMA)
        if ok:
            if obj.get("decision") == "ALLOW" and metrics is not None:
                metrics["scope_guard_allows"] = metrics.get("scope_guard_allows", 0) + 1
            if obj.get("decision") == "DENY" and metrics is not None:
                metrics["scope_guard_denials"] = metrics.get("scope_guard_denials", 0) + 1
            return obj
        if metrics is not None:
            metrics["scope_guard_failures"] = metrics.get("scope_guard_failures", 0) + 1
        return {"decision": "DENY", "reason": "Scope review output was invalid: " + "; ".join(errors[:3]), "changed_constraints": [], "preserved_constraints": []}
    except Exception as exc:
        if metrics is not None:
            metrics["scope_guard_failures"] = metrics.get("scope_guard_failures", 0) + 1
        return {"decision": "DENY", "reason": f"Scope review failed closed: {type(exc).__name__}: {exc}", "changed_constraints": [], "preserved_constraints": []}
