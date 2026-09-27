"""Canonical candidate release state for ALR legacy compatibility.

A later, weaker recovery action must not downgrade a complete deterministic result.
Python owns this invariant; the Supervisor may request more work only when the
canonical state is incomplete or genuinely conflicted.
"""
from __future__ import annotations

from datetime import datetime

from commander_agent.config import CANDIDATE_VERIFICATION_LEDGER_FILE
from commander_agent.state.evidence import unique_support_ids
from commander_agent.state.evidence_store import active_measurement_contract
from commander_agent.state.extraction import active_extraction_contract
from commander_agent.state.io import append_jsonl, stable_hash


def ensure_release_state(case_state):
    case_state.setdefault("candidate_verifications", [])
    case_state.setdefault("canonical_release_history", [])
    return case_state


def record_candidate_verification(case_state, assessment, check_evidence_id=None):
    """Persist the independent calculation verification for a contract-derived candidate."""
    ensure_release_state(case_state)
    if not isinstance(assessment, dict):
        return None
    candidate = str(assessment.get("candidate") or "").strip()
    contract_id = str(assessment.get("measurement_contract_id") or "").strip()
    if not candidate or not contract_id:
        return None
    support_ids = list(dict.fromkeys(list(assessment.get("support_ids") or []) + ([check_evidence_id] if check_evidence_id else [])))
    core = {
        "contract_id": contract_id,
        "candidate": candidate,
        "ranking_evidence_id": assessment.get("ranking_evidence_id"),
        "measurement_id": assessment.get("measurement_id"),
        "support_ids": support_ids,
        "verification_kind": assessment.get("verification_kind") or "same-source independent calculation",
        "status": "VERIFIED",
    }
    fp = stable_hash({"case_id": case_state.get("case_id"), **core})
    existing = next((x for x in case_state["candidate_verifications"] if x.get("fingerprint") == fp), None)
    if existing:
        return existing
    item = {
        "record_type": "candidate_verification",
        "case_id": case_state.get("case_id"),
        "verification_id": "CV-" + fp[:12],
        "created": datetime.now().isoformat(),
        "evidence_version": int(case_state.get("evidence_version", 0)),
        **core,
        "fingerprint": fp,
    }
    case_state["candidate_verifications"].append(item)
    append_jsonl(CANDIDATE_VERIFICATION_LEDGER_FILE, item)
    return item


def _open_conflicts(case_state):
    conflicts = []
    for bucket in ("semantic_conflicts", "evidence_conflicts"):
        for item in case_state.get(bucket, []) or []:
            if not isinstance(item, dict):
                continue
            status = str(item.get("status") or "OPEN").upper()
            if status not in {"RESOLVED", "CLOSED", "SUPERSEDED", "INVALIDATED"}:
                conflicts.append(item)
    return conflicts


def canonical_release_status(case_state, candidate=None):
    """Return whether canonical durable state is ready for final validation.

    ALR legacy compatibility supports both measurement/ranking contracts and direct-extraction
    contracts. Later weaker work cannot downgrade a ready contract unless it opens
    a real conflict or invalidates the active contract.
    """
    ensure_release_state(case_state)
    reasons = []
    conflicts = _open_conflicts(case_state)

    contract = active_measurement_contract(case_state)
    if isinstance(contract, dict):
        if str(contract.get("status") or "").upper() not in {"SEMANTICALLY_SUPPORTED", "VALIDATED"}:
            reasons.append("active measurement contract is not supported")

        checks = ((contract.get("provenance") or {}).get("completeness_checks") or {})
        required_checks = (
            "machine_rows", "rows_not_omitted", "not_truncated",
            "not_sampled_or_limited_in_spl", "below_transport_limit",
        )
        if not checks or not all(checks.get(k) is True for k in required_checks):
            reasons.append("measurement population is incomplete")

        derivations = [
            d for d in case_state.get("candidate_derivations", []) or []
            if isinstance(d, dict) and d.get("contract_id") == contract.get("contract_id")
        ]
        derivation = derivations[-1] if derivations else None
        if not derivation or derivation.get("status") != "UNIQUE_MAXIMUM" or len(derivation.get("winners") or []) != 1:
            reasons.append("no unique deterministic candidate exists under the active contract")
            derived_candidate = ""
        else:
            derived_candidate = str((derivation.get("winners") or [""])[0]).strip()

        if candidate and derived_candidate and str(candidate).strip() != derived_candidate:
            reasons.append("current candidate does not match the deterministic derivation")

        verification = next((
            v for v in reversed(case_state.get("candidate_verifications", []) or [])
            if isinstance(v, dict)
            and v.get("status") == "VERIFIED"
            and v.get("contract_id") == contract.get("contract_id")
            and str(v.get("candidate") or "").strip() == derived_candidate
        ), None)
        if not verification:
            reasons.append("deterministic candidate has no persisted independent calculation verification")
            support_ids = []
        else:
            support_ids = unique_support_ids(case_state, verification.get("support_ids") or [])
            if len(support_ids) < 2:
                reasons.append("verification does not reference two materially distinct evidence paths")

        if conflicts:
            reasons.append("open semantic/evidence conflict exists")

        ready = not reasons
        status = {
            "ready": ready,
            "candidate": derived_candidate or None,
            "contract_kind": "measurement",
            "contract_id": contract.get("contract_id"),
            "measurement_id": contract.get("measurement_id"),
            "verification_id": (verification or {}).get("verification_id"),
            "support_ids": support_ids,
            "conflict_ids": [c.get("conflict_id") or c.get("evidence_id") for c in conflicts],
            "reasons": reasons,
        }
    else:
        xcontract = active_extraction_contract(case_state)
        if not isinstance(xcontract, dict):
            return {"ready": False, "reasons": ["no active measurement or extraction contract"]}
        if str(xcontract.get("status") or "").upper() not in {"ACTIVE", "SUPPORTED", "VALIDATED"}:
            reasons.append("active extraction contract is not supported")

        derivations = [
            d for d in case_state.get("extraction_derivations", []) or []
            if isinstance(d, dict) and d.get("contract_id") == xcontract.get("extraction_contract_id")
        ]
        derivation = derivations[-1] if derivations else None
        if not derivation or derivation.get("status") != "SUPPORTED" or not str(derivation.get("candidate") or "").strip():
            reasons.append("no deterministic extraction candidate exists under the active contract")
            derived_candidate = ""
        else:
            derived_candidate = str(derivation.get("candidate") or "").strip()
        if candidate and derived_candidate and str(candidate).strip() != derived_candidate:
            reasons.append("current candidate does not match the deterministic extraction")

        verification = next((
            v for v in reversed(case_state.get("extraction_verifications", []) or [])
            if isinstance(v, dict)
            and v.get("status") == "VERIFIED"
            and v.get("contract_id") == xcontract.get("extraction_contract_id")
            and str(v.get("candidate") or "").strip() == derived_candidate
        ), None)
        if not verification:
            reasons.append("extracted candidate has no persisted independent verification")
            support_ids = []
        else:
            support_ids = unique_support_ids(case_state, verification.get("support_ids") or [])
            if len(support_ids) < 2:
                reasons.append("verification does not reference two materially distinct evidence paths")
        if conflicts:
            reasons.append("open semantic/evidence conflict exists")
        ready = not reasons
        status = {
            "ready": ready,
            "candidate": derived_candidate or None,
            "contract_kind": "extraction",
            "contract_id": xcontract.get("extraction_contract_id"),
            "verification_id": (verification or {}).get("fingerprint"),
            "support_ids": support_ids,
            "conflict_ids": [c.get("conflict_id") or c.get("evidence_id") for c in conflicts],
            "reasons": reasons,
        }

    if status.get("ready"):
        fp = stable_hash({"case_id": case_state.get("case_id"), **status})
        if not any(x.get("fingerprint") == fp for x in case_state["canonical_release_history"]):
            case_state["canonical_release_history"].append({
                "record_type": "canonical_release",
                "created": datetime.now().isoformat(),
                "evidence_version": int(case_state.get("evidence_version", 0)),
                **status,
                "fingerprint": fp,
            })
    return status
