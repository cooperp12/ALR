import json

from commander_agent.config import SUPERVISOR_NUM_CTX, MAX_SEARCH_EXECUTIONS, MIN_VALIDATION_SUPPORT
from commander_agent.state.plan import plan_for_prompt
from commander_agent.state.evidence import (
    evidence_digest,
    unique_support_ids,
    default_support_candidates,
)
from commander_agent.skills.structured_output_validation.scripts.schema import (
    VALIDATION_SCHEMA,
    parse_model_object,
)
from commander_agent.reasoning.local_ollama import structured_chat, message_content
from commander_agent.state.release import canonical_release_status


def _verified_structured_finding(case_state):
    for item in reversed(case_state.get("structured_findings", [])):
        if isinstance(item, dict) and item.get("verified") is True and item.get("candidate"):
            return item
    return None


def _candidate_matches_finding(candidate_text, finding):
    if not finding:
        return True
    expected = str(finding.get("candidate", "")).strip()
    return bool(expected and expected.lower() in (candidate_text or "").lower())


def _structured_fallback(case_state, candidate, default_support, reason):
    finding = _verified_structured_finding(case_state)
    if finding and _candidate_matches_finding(candidate, finding):
        support = list(finding.get("support_ids") or default_support[:2])
        return {
            "decision": "accept" if len(support) >= MIN_VALIDATION_SUPPORT else "continue",
            "confidence": "high" if len(support) >= MIN_VALIDATION_SUPPORT else "medium",
            "support_ids": support,
            "conflict_ids": [],
            "missing": [] if len(support) >= MIN_VALIDATION_SUPPORT else ["Need independent supporting evidence."],
            "next_action": "stop" if len(support) >= MIN_VALIDATION_SUPPORT else "Run an independent check.",
            "recommended_spl": "",
            "summary": (
                "Structured skill-derived finding agrees with the candidate using independent evidence."
                if len(support) >= MIN_VALIDATION_SUPPORT
                else "Structured resolver agrees, but support count is insufficient."
            ),
        }

    return {
        "decision": "continue",
        "confidence": "low",
        "support_ids": default_support[:2],
        "conflict_ids": [],
        "missing": [reason],
        "next_action": "Run one materially different targeted check.",
        "recommended_spl": "",
        "summary": "Validation fallback did not have enough deterministic agreement.",
    }


def validation_review(question, candidate, plan, case_state, metrics, reasoner=None, semantic_gate=None):
    """Independent final gate.

    For investigations using result_semantics_recovery, ALR legacy compatibility does not ask a second
    LLM to reinterpret the same fields. The already-executed semantic skill is the
    authoritative semantic gate; Python only verifies that it supplied a supported
    candidate and enough materially independent evidence IDs. Non-semantic workflows
    retain the general independent reviewer below.
    """
    metrics["validation_reviews"] += 1
    canonical = canonical_release_status(case_state, candidate=candidate)
    if canonical.get("ready") and str(canonical.get("candidate") or "").strip() == str(candidate or "").strip():
        return {
            "decision": "accept",
            "confidence": "high",
            "support_ids": list(canonical.get("support_ids") or []),
            "conflict_ids": [],
            "missing": [],
            "next_action": "stop",
            "recommended_spl": "",
            "summary": f"Canonical {canonical.get('contract_kind','evidence')} contract has a deterministic independently verified candidate.",
        }
    digest = evidence_digest(case_state, max_items=8, excerpt_chars=850)
    default_support = default_support_candidates(case_state)
    kind = plan.get("strategy", {}).get("investigation_type", "direct_lookup")

    if "result_semantics_recovery" in case_state.get("active_skills", []):
        if not semantic_gate:
            metrics["semantic_validation_blocks"] = metrics.get("semantic_validation_blocks", 0) + 1
            return {
                "decision": "continue",
                "confidence": "low",
                "support_ids": default_support[:2],
                "conflict_ids": [],
                "missing": ["No valid result-semantics decision is attached to this candidate."],
                "next_action": "Run the result-semantics skill before proposing another candidate.",
                "recommended_spl": "",
                "summary": "Validation cannot bypass the active result-semantics skill.",
            }

        support_ids = unique_support_ids(case_state, semantic_gate.get("support_ids", []))
        supported = (
            semantic_gate.get("decision") in {"ACCEPT", "REINTERPRET"}
            and semantic_gate.get("candidate_status") == "supported"
            and bool(str(semantic_gate.get("candidate") or "").strip())
        )
        if not supported:
            metrics["semantic_validation_blocks"] = metrics.get("semantic_validation_blocks", 0) + 1
            return {
                "decision": "continue",
                "confidence": semantic_gate.get("confidence", "low"),
                "support_ids": support_ids,
                "conflict_ids": [],
                "missing": [semantic_gate.get("reason") or "The semantic skill did not support the candidate."],
                "next_action": semantic_gate.get("next_action") or "Follow the result-semantics recovery decision.",
                "recommended_spl": semantic_gate.get("recommended_spl") or "",
                "summary": "Result-semantics skill blocked validation.",
            }

        if len(support_ids) < MIN_VALIDATION_SUPPORT:
            metrics["semantic_validation_blocks"] = metrics.get("semantic_validation_blocks", 0) + 1
            return {
                "decision": "continue",
                "confidence": semantic_gate.get("confidence", "medium"),
                "support_ids": support_ids,
                "conflict_ids": [],
                "missing": [
                    f"Need {MIN_VALIDATION_SUPPORT} materially independent supporting evidence items before validation."
                ],
                "next_action": "Run the result-semantics skill to choose a materially independent evidence action.",
                "recommended_spl": "",
                "summary": "Semantic interpretation is supported but independent evidence is insufficient.",
            }

        return {
            "decision": "accept",
            "confidence": semantic_gate.get("confidence", "high"),
            "support_ids": support_ids,
            "conflict_ids": [],
            "missing": [],
            "next_action": "stop",
            "recommended_spl": "",
            "summary": "Authoritative semantic skill supports the candidate with materially independent evidence.",
        }

    finding = _verified_structured_finding(case_state)

    if finding:
        finding_support = list(finding.get("support_ids") or [])
        default_support = finding_support + [x for x in default_support if x not in finding_support]

    if finding and not _candidate_matches_finding(candidate, finding):
        metrics["deterministic_validation_blocks"] = metrics.get("deterministic_validation_blocks", 0) + 1
        return {
            "decision": "continue",
            "confidence": "low",
            "support_ids": unique_support_ids(case_state, finding.get("support_ids", [])),
            "conflict_ids": [finding.get("finding_evidence_id")] if finding.get("finding_evidence_id") else [],
            "missing": ["Candidate conflicts with a verified structured finding derived from evidence review."],
            "next_action": "Use the structured finding or obtain genuinely new independent evidence.",
            "recommended_spl": "",
            "summary": "Candidate rejected by structured evidence consistency check.",
        }

    prompt = f'''
You are the independent CHECK/REVIEW stage of a SOC investigation harness.
Judge only the question, candidate, plan, and evidence. You do not know any expected
lab answer and must not infer one from test/scorer data.

Rules:
- WORK is not VALIDATED WORK.
- Verify every material constraint.
- For chronology, require explicit ordered evidence.
- For timeline work, require an evidence-backed T0 and correctly positioned before/after evidence.
- For external enrichment, require both the Splunk artefact and external evidence tied to it.
- If evidence conflicts, request one focused check.
- Keep the next action cheap and single-minded.

Suggested support IDs: {default_support[:4]}
Investigation type: {kind}
Verified structured finding (if any): {json.dumps(finding, ensure_ascii=False) if finding else "[none]"}

Return ONLY JSON:
{{
  "decision": "accept|continue",
  "confidence": "high|medium|low",
  "support_ids": ["E1", "E2"],
  "conflict_ids": [],
  "missing": [],
  "next_action": "one focused action if continuing",
  "recommended_spl": "optional targeted SPL or empty string",
  "summary": "short audit explanation"
}}

QUESTION: {question}
CANDIDATE: {candidate}
PLAN: {plan_for_prompt(plan)}
EVIDENCE:\n{digest or "[none]"}
SEARCH EXECUTIONS: {metrics.get('search_calls',0)} / {MAX_SEARCH_EXECUTIONS}
'''

    parsed = None

    if reasoner is not None and reasoner.available:
        text = reasoner.deep(
            prompt,
            metrics=metrics,
            purpose="validation",
            max_output_tokens=1200,
            effort="medium",
        )
        if text:
            ok, obj, errors = parse_model_object(text, VALIDATION_SCHEMA)
            if ok:
                parsed = obj
            else:
                metrics["structured_output_rejections"] = metrics.get("structured_output_rejections", 0) + 1
                print("[Remote validation JSON rejected: " + "; ".join(errors[:3]) + "]")

    if parsed is None:
        try:
            response = structured_chat(
                role="supervisor",
                messages=[{"role": "user", "content": prompt}],
                schema=VALIDATION_SCHEMA,
                metrics=metrics,
                purpose="independent_validation",
                num_ctx=min(SUPERVISOR_NUM_CTX, 4096),
                think=False,
            )
            ok, obj, errors = parse_model_object(message_content(response), VALIDATION_SCHEMA)
            if ok:
                parsed = obj
            else:
                metrics["structured_output_rejections"] = metrics.get("structured_output_rejections", 0) + 1
                print("[Local validation JSON rejected: " + "; ".join(errors[:3]) + "]")
        except Exception as exc:
            print(f"[Validation warning: {type(exc).__name__}: {exc}]")

    if not parsed:
        parsed = _structured_fallback(
            case_state,
            candidate,
            default_support,
            "Independent validation review returned unusable output.",
        )

    if not parsed.get("support_ids") and default_support:
        parsed["support_ids"] = default_support[:2]

    parsed["support_ids"] = unique_support_ids(case_state, parsed.get("support_ids", []))

    if finding and _candidate_matches_finding(candidate, finding):
        for evidence_id in finding.get("support_ids", []):
            if evidence_id not in parsed["support_ids"]:
                parsed["support_ids"].append(evidence_id)
        parsed["support_ids"] = unique_support_ids(case_state, parsed["support_ids"])

    required = MIN_VALIDATION_SUPPORT
    if parsed.get("decision") == "accept" and len(parsed["support_ids"]) < required and metrics.get("search_calls", 0) < MAX_SEARCH_EXECUTIONS:
        parsed["decision"] = "continue"
        parsed.setdefault("missing", []).append(
            f"Need {required} materially independent supporting evidence items before validation."
        )
        if not parsed.get("next_action"):
            parsed["next_action"] = "Cross-check using a materially different method."
    return parsed


def runtime_guidance(plan, prior_context, timeline_context="", strategy_guidance=""):
    return f'''
HARNESS PLAN
------------
{plan_for_prompt(plan)}

WORK / VALIDATION POLICY
------------------------
- Cached work is cheap, but cached work is not automatically validated work.
- Reuse the local timeline/case map before asking Splunk for the same facts again.
- Primary DO work must obey active skill query contracts.
- CHECK must use a materially different method.
- Retry failures/non-findings with a changed strategy.
- Structured JSON/tool/search results are validated before they are cached or persisted.
- A failed/malformed result is failure telemetry, not evidence.

SKILL STRATEGY
--------------
{strategy_guidance or "[none]"}

PRIOR VALIDATED CONTEXT
-----------------------
{prior_context or "[none]"}

REUSABLE TIMELINE / CASE MAP
----------------------------
{timeline_context or "[none]"}
'''


def validation_feedback_message(review):
    pieces = [
        "VALIDATION REVIEW: candidate is not yet accepted.",
        f"Confidence: {review.get('confidence', 'low')}",
        f"Audit: {review.get('summary', '')}",
    ]
    if review.get("missing"):
        pieces.append("Missing/check needed: " + "; ".join(map(str, review["missing"])))
    if review.get("next_action"):
        pieces.append("NEXT SINGLE-MINDED ACTION: " + str(review["next_action"]))
    if review.get("recommended_spl"):
        pieces.append("Reviewer SPL hint (validate/adapt before use): " + str(review["recommended_spl"]))
    pieces.append("Continue using tools. Do not repeat a successful query unchanged.")
    return "\n".join(pieces)
