import json
import re
from datetime import datetime
from pathlib import Path

from commander_agent.config import EVIDENCE_LEDGER_FILE, EVAL_LEDGER_FILE, SEMANTIC_FINDINGS_FILE, WORK_VERSION
from commander_agent.mcp.results import clip_text
from commander_agent.state.cache import result_event_count, normalise_spl_for_cache
from commander_agent.state.io import append_jsonl, stable_hash
from commander_agent.state.timeline import append_timeline_events, maybe_auto_anchor, current_anchor
from commander_agent.skills.structured_output_validation.scripts.schema import parse_json_text


def question_terms(question):
    stop = {
        "what", "when", "where", "which", "that", "this", "with", "from",
        "using", "does", "into", "most", "name", "full", "answer", "attempt",
        "specific", "resource", "user", "question", "adversary",
    }
    terms = []
    for token in re.findall(r"[a-z0-9_.:/-]+", (question or "").lower()):
        if len(token) >= 5 and token not in stop and token not in terms:
            terms.append(token)
    return terms[:20]


def load_evidence_ledger():
    records = []
    p = Path(EVIDENCE_LEDGER_FILE)
    if not p.exists():
        return records
    try:
        with p.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        records.append(obj)
                except Exception:
                    continue
    except Exception:
        pass
    return records


def prior_validated_context(question, max_items=4):
    records = load_evidence_ledger()
    validated_pairs = set()
    for record in records:
        if record.get("record_type") == "validation" and record.get("outcome") == "validated":
            case_id = record.get("case_id")
            for evidence_id in record.get("support_ids", []):
                validated_pairs.add((case_id, evidence_id))

    terms = question_terms(question)
    if not terms or not validated_pairs:
        return ""

    scored = []
    for record in records:
        if record.get("record_type") != "evidence":
            continue
        if (record.get("case_id"), record.get("evidence_id")) not in validated_pairs:
            continue
        haystack = (str(record.get("query", "")) + " " + str(record.get("result_excerpt", ""))).lower()
        score = sum(1 for term in terms if term in haystack)
        if score:
            scored.append((score, record))

    scored.sort(key=lambda item: item[0], reverse=True)
    lines = []
    for score, record in scored[:max_items]:
        lines.append(
            f"- prior validated {record.get('case_id')}/{record.get('evidence_id')} "
            f"(match={score}, quality={record.get('quality', 0):.2f}, method={record.get('method_class','')}): "
            f"{clip_text(record.get('result_excerpt', ''), 1000)}"
        )
    return "\n".join(lines)


def new_case_state(plan, active_skills=None):
    case_id = plan.get("case_id")
    return {
        "case_id": case_id,
        "question": plan.get("question", ""),
        "active_skills": list(active_skills or []),
        "evidence": [],
        "signature_to_evidence": {},
        "timeline_rows": 0,
        "failed_query_shapes": set(),
        "anchor": current_anchor(case_id),
        "structured_findings": [],
        "semantic_reviews": [],
        # ALR legacy compatibility: data evidence advances a monotonic version. Derived reasoning does not.
        "evidence_version": 0,
        # ALR legacy compatibility: deterministic state transitions (derivation/verification) count as analytical progress
        # even when they intentionally confirm an already-observed value.
        "analytical_progress_version": 0,
        "analytical_progress_events": [],
        "semantic_findings": [],
        "candidate_quarantine": [],
        "semantic_action_signatures": set(),
        "semantic_reviews_by_version": {},
        # ALR legacy compatibility semantic-planning state. These are populated before the first query.
        "investigation_frame": dict(plan.get("investigation_frame") or {}) or None,
        "semantic_bindings": [dict(x) for x in (plan.get("semantic_bindings") or [])],
        "semantic_conflicts": [],
        "semantic_relationships": [],
        "source_contract": dict(plan.get("source_contract") or {}) or None,
        "extraction_contracts": ([dict(plan.get("extraction_contract"))] if plan.get("extraction_contract") else []),
        "extraction_derivations": [],
        "extraction_verifications": [],
        "discovery_state": {"observed_event_names": []},
        "bypass_persistent_cache": bool(plan.get("bypass_persistent_cache", False)),
    }


def evidence_model_text(evidence_id, cache_label, model_result, extra_note=""):
    header = [
        f"EVIDENCE_ID: {evidence_id}",
        f"WORK_CACHE: {cache_label}",
        "VALIDATION_STATUS: WORK (not yet promoted to validated work)",
    ]
    if extra_note:
        header.append(extra_note)
    return "\n".join(header) + "\n\n" + model_result


def _evidence_quality(source, method_class, cache_label):
    if source == "splunk" and method_class in {
        "aggregation_distinct", "aggregation_values", "chronological_first", "targeted_rows"
    }:
        base = 0.95
    elif source == "skill_reasoning":
        base = 0.90
    elif source == "deterministic":
        base = 0.97
    elif source == "splunk":
        base = 0.82
    elif source == "analytics":
        base = 0.96
    elif source == "external":
        base = 0.88
    elif source == "timeline":
        base = 0.82
    else:
        base = 0.65
    if "cached" in (cache_label or ""):
        base -= 0.03
    return max(0.0, min(1.0, base))


def record_evidence(
    case_state,
    source,
    tool_name,
    model_result,
    query="",
    signature=None,
    cache_label="fresh",
    method_class="",
):
    if signature and signature in case_state["signature_to_evidence"]:
        existing_id = case_state["signature_to_evidence"][signature]
        for record in case_state["evidence"]:
            if record.get("evidence_id") == existing_id:
                return record

    evidence_id = f"E{len(case_state['evidence']) + 1}"
    quality = _evidence_quality(source, method_class, cache_label)
    # Only independent/data-bearing evidence advances the evidence version.
    # Skill reasoning and deterministic summaries are derived from existing data and
    # must not unlock quarantined candidates or reset stagnation guards.
    if source not in {"skill_reasoning", "deterministic"}:
        case_state["evidence_version"] = int(case_state.get("evidence_version", 0)) + 1
    evidence_version = int(case_state.get("evidence_version", 0))
    record = {
        "record_type": "evidence",
        "case_id": case_state["case_id"],
        "evidence_id": evidence_id,
        "created": datetime.now().isoformat(),
        "source": source,
        "tool": tool_name,
        "query": query,
        "signature": signature,
        "method_class": method_class,
        "quality": quality,
        "independence_key": stable_hash(
            {
                "source": source,
                "tool": tool_name,
                "method_class": method_class,
                "query": normalise_spl_for_cache(query),
            }
        ),
        "cache_label": cache_label,
        "event_count": result_event_count(model_result),
        "result_excerpt": clip_text(model_result, 6000),
        "validation_status": "work",
        "evidence_version": evidence_version,
    }
    case_state["evidence"].append(record)
    if signature:
        case_state["signature_to_evidence"][signature] = evidence_id
    append_jsonl(EVIDENCE_LEDGER_FILE, record)

    ok, parsed, _ = parse_json_text(model_result, expected_type=dict, allow_embedded=False)
    if ok:
        events = parsed.get("events", parsed.get("results"))
        if isinstance(events, list):
            count, rows = append_timeline_events(case_state, record, events)
            case_state["timeline_rows"] += count
            if "bidirectional_timeframe" in case_state.get("active_skills", []):
                auto = maybe_auto_anchor(case_state, record, rows)
                if auto and auto.get("ok"):
                    case_state["anchor"] = auto["anchor"]
    return record


def record_structured_finding(case_state, finding, support_ids=None, method_class="structured_resolution", source="deterministic"):
    """Persist a structured fact derived from evidence, never from test answers."""
    if not isinstance(finding, dict):
        return None
    payload = json.dumps(finding, ensure_ascii=False, indent=2)
    signature = stable_hash({"case_id": case_state.get("case_id"), "finding": finding})
    record = record_evidence(
        case_state,
        source=source,
        tool_name="structured_resolver",
        model_result=payload,
        query="",
        signature=signature,
        cache_label="derived work",
        method_class=method_class,
    )
    item = dict(finding)
    item["evidence_id"] = record.get("evidence_id") if record else None
    item["support_ids"] = list(support_ids or [])
    case_state.setdefault("structured_findings", []).append(item)
    return record


def record_semantic_review(case_state, review, support_ids=None):
    """Persist the result-semantics skill output as derived WORK.

    It is not counted as an independent evidence path because it is reasoning over
    existing evidence rather than a separate data source.
    """
    if not isinstance(review, dict):
        return None
    payload = json.dumps(review, ensure_ascii=False, indent=2)
    signature = stable_hash({
        "case_id": case_state.get("case_id"),
        "semantic_review": review,
        "support_ids": list(support_ids or []),
    })
    record = record_evidence(
        case_state,
        source="skill_reasoning",
        tool_name="result_semantics_recovery",
        model_result=payload,
        query="",
        signature=signature,
        cache_label="derived work",
        method_class="result_semantics_recovery",
    )
    item = dict(review)
    item["evidence_id"] = record.get("evidence_id") if record else None
    item["support_ids"] = list(support_ids or review.get("support_ids") or [])
    item["evidence_version"] = int(case_state.get("evidence_version", 0))
    case_state.setdefault("semantic_reviews", []).append(item)
    return record



def current_evidence_version(case_state):
    return int(case_state.get("evidence_version", 0))


def mark_analytical_progress(case_state, kind, fingerprint="", details=None):
    """Record a durable *state* advance that may not add new raw evidence.

    Verification often confirms an existing value.  ALR legacy compatibility treats that as progress so
    the no-new-evidence guard cannot kill a case between deterministic derivation and
    verification/release.  Duplicate progress fingerprints are idempotent.
    """
    events = case_state.setdefault("analytical_progress_events", [])
    key = str(fingerprint or stable_hash({"kind": kind, "details": details or {}}))
    if any(str(x.get("fingerprint") or "") == key for x in events if isinstance(x, dict)):
        return False
    events.append({"kind": str(kind), "fingerprint": key, "details": dict(details or {})})
    case_state["analytical_progress_version"] = int(case_state.get("analytical_progress_version", 0)) + 1
    return True


def current_progress_version(case_state):
    """Combined raw-evidence + analytical-state token for stagnation detection."""
    return (
        int(case_state.get("evidence_version", 0)),
        int(case_state.get("analytical_progress_version", 0)),
    )


def _normalise_candidate(candidate):
    return re.sub(r"\s+", " ", str(candidate or "").strip().lower())


def candidate_fingerprint(candidate):
    normalised = _normalise_candidate(candidate)
    return stable_hash({"candidate": normalised}) if normalised else ""


def quarantine_candidate(case_state, candidate, review=None):
    """Block a rejected candidate until genuinely new data evidence arrives."""
    fingerprint = candidate_fingerprint(candidate)
    if not fingerprint:
        return None
    item = {
        "candidate_fingerprint": fingerprint,
        "candidate_excerpt": clip_text(str(candidate), 600),
        "rejected_at_evidence_version": current_evidence_version(case_state),
        "reason": str((review or {}).get("reason") or (review or {}).get("issue_type") or "semantic rejection"),
        "decision": str((review or {}).get("decision") or "REJECT_CANDIDATE"),
        "created": datetime.now().isoformat(),
    }
    existing = case_state.setdefault("candidate_quarantine", [])
    if not any(
        x.get("candidate_fingerprint") == fingerprint
        and x.get("rejected_at_evidence_version") == item["rejected_at_evidence_version"]
        for x in existing
    ):
        existing.append(item)
    return item


def candidate_is_quarantined(case_state, candidate):
    fingerprint = candidate_fingerprint(candidate)
    version = current_evidence_version(case_state)
    return bool(fingerprint and any(
        x.get("candidate_fingerprint") == fingerprint
        and int(x.get("rejected_at_evidence_version", -1)) == version
        for x in case_state.get("candidate_quarantine", [])
    ))


def semantic_review_budget_available(case_state, max_per_version):
    version = current_evidence_version(case_state)
    counts = case_state.setdefault("semantic_reviews_by_version", {})
    return int(counts.get(str(version), counts.get(version, 0))) < int(max_per_version)


def bump_semantic_review_budget(case_state):
    version = current_evidence_version(case_state)
    counts = case_state.setdefault("semantic_reviews_by_version", {})
    key = str(version)
    counts[key] = int(counts.get(key, 0)) + 1
    return counts[key]


def record_semantic_finding(case_state, review, support_ids=None, status="pending_action"):
    """Persist a first-class semantic state transition as JSONL.

    This is durable reasoning state, not independent evidence. Repeated status changes
    append new JSONL events so the history is auditable.
    """
    if not isinstance(review, dict):
        return None
    index = len(case_state.setdefault("semantic_findings", [])) + 1
    finding_id = review.get("finding_id") or f"S{index}"
    item = {
        "record_type": "semantic_finding",
        "case_id": case_state.get("case_id"),
        "finding_id": finding_id,
        "created": datetime.now().isoformat(),
        "evidence_version": current_evidence_version(case_state),
        "support_ids": list(support_ids or review.get("support_ids") or []),
        "decision": review.get("decision"),
        "candidate_status": review.get("candidate_status"),
        "candidate": review.get("candidate"),
        "reason": review.get("reason", ""),
        "recommended_spl": review.get("recommended_spl"),
        "analysis_request": review.get("analysis_request"),
        "scope_change": review.get("scope_change"),
        "status": status,
    }
    case_state["semantic_findings"].append(item)
    append_jsonl(SEMANTIC_FINDINGS_FILE, item)
    return item


def record_semantic_finding_status(case_state, finding_id, status, note=""):
    item = {
        "record_type": "semantic_finding_status",
        "case_id": case_state.get("case_id"),
        "finding_id": finding_id,
        "created": datetime.now().isoformat(),
        "evidence_version": current_evidence_version(case_state),
        "status": status,
        "note": note,
    }
    case_state.setdefault("semantic_findings", []).append(item)
    append_jsonl(SEMANTIC_FINDINGS_FILE, item)
    return item

def evidence_digest(case_state, max_items=10, excerpt_chars=1000):
    """Project relevant evidence into context instead of using chronology alone.

    Active measurement support and semantic-conflict evidence remain visible even
    after later queries arrive. Recent evidence still receives a small recency boost.
    """
    all_records = list(case_state.get("evidence", []))
    support = set()
    contract = case_state.get("active_measurement_contract") or {}
    support.update(contract.get("support_ids") or [])
    for item in case_state.get("semantic_conflicts", [])[-5:]:
        support.update(item.get("support_ids") or [])
    for item in case_state.get("structured_findings", [])[-5:]:
        support.update(item.get("support_ids") or [])
    scored=[]
    total=max(1,len(all_records))
    for i,record in enumerate(all_records):
        eid=record.get("evidence_id")
        score=float(record.get("quality") or 0)*10.0 + (i+1)/total
        if eid in support: score += 100.0
        if record.get("source") not in {"skill_reasoning","deterministic"}: score += 2.0
        scored.append((score,i,record))
    chosen=sorted(scored, key=lambda x:(x[0],x[1]), reverse=True)[:max_items]
    # Render selected evidence in original order to preserve investigation chronology.
    records=[x[2] for x in sorted(chosen,key=lambda x:x[1])]
    lines = []
    for record in records:
        lines.append(
            f"{record.get('evidence_id')} | source={record.get('source')} "
            f"| method={record.get('method_class')} | quality={record.get('quality')} "
            f"| cache={record.get('cache_label')} | event_count={record.get('event_count')} "
            f"| query={clip_text(record.get('query', ''), 420)}\n"
            f"{clip_text(record.get('result_excerpt', ''), excerpt_chars)}"
        )
    if case_state.get("structured_findings"):
        lines.append(
            "STRUCTURED FINDINGS\n"
            + clip_text(json.dumps(case_state["structured_findings"][-3:], ensure_ascii=False, indent=2), 1800)
        )
    return "\n\n".join(lines)


def unique_support_ids(case_state, support_ids):
    known = {record.get("evidence_id"): record for record in case_state.get("evidence", [])}
    unique = []
    signatures = set()
    method_classes = set()

    for evidence_id in support_ids or []:
        record = known.get(evidence_id)
        if not record:
            continue
        signature = record.get("independence_key") or record.get("signature") or evidence_id
        method = record.get("method_class") or "unknown"
        if signature in signatures:
            continue
        if method in {
            "aggregation_distinct", "aggregation_values", "chronological_first", "targeted_rows"
        } and method in method_classes:
            continue
        signatures.add(signature)
        method_classes.add(method)
        unique.append(evidence_id)
    return unique


def default_support_candidates(case_state):
    records = sorted(case_state.get("evidence", []), key=lambda r: r.get("quality", 0), reverse=True)
    support = []
    seen_methods = set()
    for record in records:
        # Deterministic resolver records are summaries derived from other evidence;
        # they are useful context but are not counted as an independent support path.
        if record.get("source") in {"deterministic", "skill_reasoning"}:
            continue
        method = record.get("method_class") or record.get("independence_key")
        if method in seen_methods:
            continue
        if record.get("event_count") == 0:
            continue
        support.append(record.get("evidence_id"))
        seen_methods.add(method)
    return support


def append_validation_record(case_state, candidate, review, outcome):
    append_jsonl(
        EVIDENCE_LEDGER_FILE,
        {
            "record_type": "validation",
            "case_id": case_state["case_id"],
            "created": datetime.now().isoformat(),
            "outcome": outcome,
            "candidate": candidate,
            "support_ids": review.get("support_ids", []),
            "conflict_ids": review.get("conflict_ids", []),
            "confidence": review.get("confidence"),
            "summary": review.get("summary"),
            "missing": review.get("missing", []),
        },
    )


def append_eval_run(question, answer, metrics, skills, scorer_passed):
    append_jsonl(
        EVAL_LEDGER_FILE,
        {
            "record_type": "eval_run",
            "created": datetime.now().isoformat(),
            "work_version": WORK_VERSION,
            "question": question,
            "answer": answer,
            "scorer_passed": scorer_passed,
            "skills": list(skills),
            "metrics": metrics,
        },
    )
