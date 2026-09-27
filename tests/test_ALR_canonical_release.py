import json

import commander_agent.state.evidence_store as store
import commander_agent.state.release as release
from commander_agent.skills.investigation_supervisor.scripts.reviewer import _deterministic_guard


def _disable_disk(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "OBSERVATION_LEDGER_FILE", tmp_path / "obs.jsonl")
    monkeypatch.setattr(store, "MEASUREMENT_CONTRACT_LEDGER_FILE", tmp_path / "contracts.jsonl")
    monkeypatch.setattr(store, "CANDIDATE_DERIVATION_LEDGER_FILE", tmp_path / "cand.jsonl")
    monkeypatch.setattr(store, "CASE_STATE_DB_FILE", tmp_path / "state.sqlite")
    monkeypatch.setattr(release, "CANDIDATE_VERIFICATION_LEDGER_FILE", tmp_path / "candidate_verifications.jsonl")


def _evidence(eid, query, rows, method, key):
    return {
        "evidence_id": eid,
        "source": "splunk",
        "query": query,
        "machine_result": {"events": rows, "rows_omitted": 0},
        "result_excerpt": json.dumps({"events": rows}),
        "result_limit": 50,
        "method_class": method,
        "independence_key": key,
        "signature": key,
        "event_count": len(rows),
    }


def _verified_q3_state(monkeypatch, tmp_path):
    _disable_disk(monkeypatch, tmp_path)
    state = {
        "case_id": "ALR-q3-release",
        "question": "What IAM user access key generates the most distinct errors when attempting to access IAM resources?",
        "evidence_version": 2,
        "evidence": [],
        "semantic_conflicts": [],
        "evidence_conflicts": [],
    }
    rows = [
        {"userIdentity.accessKeyId": "A", "distinct_count": "1", "matching_events": "9", "population_keys": "3"},
        {"userIdentity.accessKeyId": "B", "distinct_count": "5", "matching_events": "6", "population_keys": "3"},
        {"userIdentity.accessKeyId": "C", "distinct_count": "1", "matching_events": "2", "population_keys": "3"},
    ]
    q1 = "index=botsv3 | stats dc(errorMessage) AS distinct_count count AS matching_events BY userIdentity.accessKeyId | eventstats count AS population_keys | sort 0 userIdentity.accessKeyId"
    e1 = _evidence("E1", q1, rows, "aggregation_distinct", "primary")
    state["evidence"].append(e1)
    store.ingest_machine_evidence(state, e1, e1["machine_result"])
    contract = store.create_measurement_contract(
        state,
        requested_quantity=state["question"],
        evidence_id="E1",
        group_field="userIdentity.accessKeyId",
        metric_field="distinct_count",
        source_expression="errorMessage",
        support_ids=["E1"],
        reason="Detailed error descriptions are the bound measurement.",
        confidence="high",
    )
    assessment = store.assessment_from_active_contract(state)
    assert assessment["candidate"] == "B"

    # Independent grouped-pair calculation evidence.
    q2 = "index=botsv3 | stats count BY userIdentity.accessKeyId errorMessage | stats count AS checked_count BY userIdentity.accessKeyId | eventstats count AS population_keys | sort 0 userIdentity.accessKeyId"
    check_rows = [
        {"userIdentity.accessKeyId": "A", "checked_count": "1", "population_keys": "3"},
        {"userIdentity.accessKeyId": "B", "checked_count": "5", "population_keys": "3"},
        {"userIdentity.accessKeyId": "C", "checked_count": "1", "population_keys": "3"},
    ]
    e2 = _evidence("E2", q2, check_rows, "aggregation_other", "grouped-pair")
    state["evidence"].append(e2)
    verified = {
        **assessment,
        "support_ids": ["E1", "E2"],
        "calculation_verified": True,
        "verification_kind": "same-source independent calculation",
    }
    release.record_candidate_verification(state, verified, "E2")
    return state, contract, verified


def _deny_obj():
    return {
        "evaluation": "PASS",
        "goal_satisfied": False,
        "remaining_gap": "Run another recovery query.",
        "next_capabilities": ["alternative_aggregation"],
        "irrelevant_capabilities": [],
        "invalidate_from_stage": None,
        "allow_validation": False,
        "allow_stop": False,
        "reason": "Model wants more evidence.",
    }


def test_verified_canonical_candidate_overrides_supervisor_recovery_request(monkeypatch, tmp_path):
    state, _, _ = _verified_q3_state(monkeypatch, tmp_path)
    status = release.canonical_release_status(state, candidate="B")
    assert status["ready"] is True
    guarded = _deterministic_guard(_deny_obj(), state, candidate="B")
    assert guarded["allow_validation"] is True
    assert guarded["goal_satisfied"] is True
    assert guarded["next_capabilities"] == []
    assert guarded["canonical_release_ready"] is True


def test_later_weaker_head1_evidence_does_not_downgrade_canonical_release(monkeypatch, tmp_path):
    state, _, _ = _verified_q3_state(monkeypatch, tmp_path)
    weak = _evidence(
        "E3",
        "index=botsv3 | stats dc(errorMessage) AS distinct_count BY userIdentity.accessKeyId | sort - distinct_count | head 1",
        [{"userIdentity.accessKeyId": "B", "distinct_count": "5"}],
        "aggregation_distinct",
        "weak-head1",
    )
    state["evidence"].append(weak)
    state["evidence_version"] = 3
    # This evidence is intentionally not used to replace the complete E1 contract.
    assert release.canonical_release_status(state, candidate="B")["ready"] is True
    guarded = _deterministic_guard(_deny_obj(), state, candidate="B")
    assert guarded["allow_validation"] is True


def test_real_open_conflict_blocks_canonical_release(monkeypatch, tmp_path):
    state, _, _ = _verified_q3_state(monkeypatch, tmp_path)
    state["semantic_conflicts"].append({
        "conflict_id": "SC-real",
        "status": "OPEN",
        "type": "SEMANTIC_BINDING_CONFLICT",
        "reason": "New evidence genuinely contradicts the active measurement semantics.",
    })
    status = release.canonical_release_status(state, candidate="B")
    assert status["ready"] is False
    assert "open semantic/evidence conflict exists" in status["reasons"]
    guarded = _deterministic_guard(_deny_obj(), state, candidate="B")
    assert guarded["allow_validation"] is False
