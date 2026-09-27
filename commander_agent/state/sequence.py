from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable

VALIDATED_STATUSES = {"validated", "validated_cache_hit"}


def _fact_id(qid: str, role: str, value: str) -> str:
    raw = json.dumps({"qid": qid, "role": role, "value": value}, sort_keys=True)
    return "FACT-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _add_fact(facts: list[dict[str, Any]], *, qid: str, role: str, value: str,
              question: str, support_ids: list[str], evidence: list[dict[str, Any]],
              confidence: str = "high", source: str = "validated_answer") -> None:
    value = str(value or "").strip()
    if not value:
        return
    item = {
        "fact_id": _fact_id(qid, role, value),
        "qid": qid,
        "role": role,
        "value": value,
        "question": question,
        "support_ids": list(support_ids or []),
        "evidence": list(evidence or []),
        "confidence": confidence,
        "source": source,
    }
    if not any(x.get("fact_id") == item["fact_id"] for x in facts):
        facts.append(item)


def _infer_answer_facts(qid: str, question: str, answer: str, support_ids, evidence):
    facts: list[dict[str, Any]] = []
    qlow = str(question or "").lower()
    _add_fact(facts, qid=qid, role="validated_answer", value=answer, question=question,
              support_ids=support_ids, evidence=evidence)

    for key in sorted(set(re.findall(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b", answer or ""))):
        _add_fact(facts, qid=qid, role="aws_access_key", value=key, question=question,
                  support_ids=support_ids, evidence=evidence)

    if "support case" in qlow or "case id" in qlow:
        for value in re.findall(r"\b[0-9]{8,14}\b", answer or ""):
            _add_fact(facts, qid=qid, role="support_case_id", value=value, question=question,
                      support_ids=support_ids, evidence=evidence)

    if "user agent" in qlow and answer.strip():
        _add_fact(facts, qid=qid, role="user_agent", value=answer.strip(), question=question,
                  support_ids=support_ids, evidence=evidence)

    return facts


def build_validated_sequence_state(results: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Promote only evidence-validated findings into reusable sequence state.

    The state is generic: every validated answer becomes a typed fact and keeps
    support/evidence provenance.  Incident-specific aliases are derived from fact
    roles for backward compatibility, never from scorer expected values.
    """
    state: dict[str, Any] = {
        "schema_version": 2,
        "validated_qids": [],
        "facts": [],
        "provenance": {},
    }
    for result in results:
        metrics = result.get("metrics") or {}
        if metrics.get("final_validation_status") not in VALIDATED_STATUSES:
            continue
        qid = str(result.get("qid") or "").strip()
        question = str(result.get("question") or "")
        answer = str(result.get("answer") or "").strip()
        if not qid or not answer:
            continue
        support_ids = list(metrics.get("validated_support_ids") or [])
        evidence = list(metrics.get("validated_sequence_evidence") or [])
        state["validated_qids"].append(qid)
        state["provenance"][qid] = {
            "question": question,
            "answer": answer,
            "validation": metrics.get("final_validation_status"),
            "support_ids": support_ids,
            "evidence": evidence,
        }
        supplied = metrics.get("validated_facts") or []
        if isinstance(supplied, list):
            for fact in supplied:
                if not isinstance(fact, dict):
                    continue
                _add_fact(
                    state["facts"], qid=qid,
                    role=str(fact.get("role") or "validated_fact"),
                    value=str(fact.get("value") or ""), question=question,
                    support_ids=support_ids, evidence=evidence,
                    confidence=str(fact.get("confidence") or "high"),
                    source="validated_fact",
                )
        inferred = _infer_answer_facts(qid, question, answer, support_ids, evidence)
        # Compatibility for legacy callers that did not retain question text.
        # This assigns semantic roles only from answer shape + sequence label; it
        # does not inject any expected benchmark value.
        if not question:
            if qid == "Q1" and re.fullmatch(r"[0-9]{8,14}", answer):
                _add_fact(inferred, qid=qid, role="support_case_id", value=answer, question=question,
                          support_ids=support_ids, evidence=evidence)
            elif qid == "Q2":
                _add_fact(inferred, qid=qid, role="user_agent", value=answer, question=question,
                          support_ids=support_ids, evidence=evidence)
        for fact in inferred:
            if not any(x.get("fact_id") == fact["fact_id"] for x in state["facts"]):
                state["facts"].append(fact)

    # Compatibility aliases are projections from the generic fact collection.
    role_aliases = {
        "support_case_id": "support_case_id",
        "user_agent": "adversary_user_agent",
    }
    for role, alias in role_aliases.items():
        vals = sequence_values(state, role)
        if vals:
            state[alias] = vals[-1]
    keys = sequence_values(state, "aws_access_key")
    if keys:
        state["compromised_access_key"] = keys[-1]
    return state


def sequence_facts(state: dict[str, Any], *roles: str) -> list[dict[str, Any]]:
    wanted = {str(x) for x in roles if str(x)}
    facts = [x for x in (state.get("facts") or []) if isinstance(x, dict)]
    return [x for x in facts if not wanted or x.get("role") in wanted]


def sequence_values(state: dict[str, Any], *roles: str) -> list[str]:
    values = []
    for fact in sequence_facts(state, *roles):
        value = str(fact.get("value") or "").strip()
        if value and value not in values:
            values.append(value)
    return values


def render_validated_sequence_state(state: dict[str, Any]) -> str:
    compact = {
        "schema_version": state.get("schema_version", 2),
        "validated_qids": state.get("validated_qids", []),
        "facts": [
            {k: fact.get(k) for k in ("fact_id", "qid", "role", "value", "confidence", "support_ids")}
            for fact in sequence_facts(state)
        ],
    }
    return (
        "VALIDATED INCIDENT-SEQUENCE STATE FROM EARLIER QUESTIONS:\n"
        + json.dumps(compact, indent=2, ensure_ascii=False)
        + "\nUse these as established evidence-backed facts. Do not rediscover them unless contradictory machine evidence appears."
    )
