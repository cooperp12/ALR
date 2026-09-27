import json

import commander_agent.state.evidence_store as store
from commander_agent.state.candidate_checks import candidate_errors


def _state():
    return {
        "case_id": "q3-test",
        "question": "What IAM user access key generates the most distinct errors when attempting to access IAM resources?",
        "evidence_version": 4,
        "evidence": [],
    }


def _evidence(eid, query, rows, limit=50):
    return {
        "evidence_id": eid,
        "source": "splunk",
        "query": query,
        "machine_result": {"events": rows, "rows_omitted": 0},
        "result_excerpt": json.dumps({"events": rows, "rows_omitted": 0}),
        "result_limit": limit,
    }


def _disable_disk(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "OBSERVATION_LEDGER_FILE", tmp_path / "obs.jsonl")
    monkeypatch.setattr(store, "MEASUREMENT_CONTRACT_LEDGER_FILE", tmp_path / "contracts.jsonl")
    monkeypatch.setattr(store, "CANDIDATE_DERIVATION_LEDGER_FILE", tmp_path / "candidates.jsonl")
    monkeypatch.setattr(store, "CASE_STATE_DB_FILE", tmp_path / "state.sqlite")


def test_q3_observations_survive_later_coarse_evidence(monkeypatch, tmp_path):
    _disable_disk(monkeypatch, tmp_path)
    state = _state()
    e4_rows = [
        {"userIdentity.accessKeyId":"A","code_count":"1","message_count":"1","operation_error_count":"1"},
        {"userIdentity.accessKeyId":"B","code_count":"1","message_count":"5","operation_error_count":"5"},
        {"userIdentity.accessKeyId":"C","code_count":"1","message_count":"1","operation_error_count":"1"},
    ]
    q4 = ('index=botsv3 | eval operation_error=json_object("operation",eventName,"code",errorCode,"message",errorMessage) '
          '| stats dc(errorCode) AS code_count dc(errorMessage) AS message_count '
          'dc(operation_error) AS operation_error_count BY userIdentity.accessKeyId')
    e4 = _evidence("E4", q4, e4_rows)
    state["evidence"].append(e4)
    store.ingest_machine_evidence(state, e4, e4["machine_result"])

    before = [o for o in state["observations"] if o.get("evidence_id")=="E4" and o.get("metric_field")=="operation_error_count" and o.get("group_value")=="B"]
    assert before and before[0]["value"] == 5

    # E7 later returns the coarse errorCode tie. It must not erase E4's facts.
    e7_rows = [
        {"userIdentity.accessKeyId":"A","distinct_count":"1"},
        {"userIdentity.accessKeyId":"B","distinct_count":"1"},
        {"userIdentity.accessKeyId":"C","distinct_count":"1"},
    ]
    q7 = 'index=botsv3 | stats dc(errorCode) AS distinct_count BY userIdentity.accessKeyId'
    e7 = _evidence("E7", q7, e7_rows)
    state["evidence"].append(e7)
    store.ingest_machine_evidence(state, e7, e7["machine_result"])

    after = [o for o in state["observations"] if o.get("evidence_id")=="E4" and o.get("metric_field")=="operation_error_count" and o.get("group_value")=="B"]
    assert after and after[0]["value"] == 5
    assert any(c["metric_field"]=="operation_error_count" and c["values"]["B"]==5 for c in store.measurement_candidates(state))


def test_measurement_contract_derives_unique_candidate_without_model(monkeypatch, tmp_path):
    _disable_disk(monkeypatch, tmp_path)
    state = _state()
    rows = [
        {"userIdentity.accessKeyId":"A","message_count":"1"},
        {"userIdentity.accessKeyId":"B","message_count":"5"},
        {"userIdentity.accessKeyId":"C","message_count":"1"},
    ]
    query = 'index=botsv3 | stats dc(errorMessage) AS message_count BY userIdentity.accessKeyId'
    e4 = _evidence("E4", query, rows)
    state["evidence"].append(e4)
    store.ingest_machine_evidence(state, e4, e4["machine_result"])
    contract = store.create_measurement_contract(
        state, requested_quantity="most distinct errors", evidence_id="E4",
        group_field="userIdentity.accessKeyId", metric_field="message_count",
        source_expression="errorMessage", support_ids=["E4"],
        reason="Distinct error messages represent the requested distinct errors.", confidence="high")
    assessment = store.assessment_from_active_contract(state)
    assert contract["status"] == "SEMANTICALLY_SUPPORTED"
    assert assessment["status"] == "SUPPORTED"
    assert assessment["candidate"] == "B"
    assert assessment["maximum_value"] == 5
    assert candidate_errors(assessment, state) == []


def test_tied_maximum_is_preserved_not_lexically_broken(monkeypatch, tmp_path):
    _disable_disk(monkeypatch, tmp_path)
    state = _state()
    rows = [
        {"userIdentity.accessKeyId":"A","message_count":"5"},
        {"userIdentity.accessKeyId":"B","message_count":"5"},
        {"userIdentity.accessKeyId":"C","message_count":"1"},
    ]
    query='index=botsv3 | stats dc(errorMessage) AS message_count BY userIdentity.accessKeyId'
    ev=_evidence("E4",query,rows); state["evidence"].append(ev); store.ingest_machine_evidence(state,ev,ev["machine_result"])
    store.create_measurement_contract(state,requested_quantity="most distinct errors",evidence_id="E4",group_field="userIdentity.accessKeyId",metric_field="message_count",source_expression="errorMessage",support_ids=["E4"],reason="semantic choice")
    assessment=store.assessment_from_active_contract(state)
    assert assessment["status"] == "AMBIGUOUS"
    assert assessment["candidate"] is None
    assert assessment["tie"] is True
    assert assessment["winners"] == ["A","B"]


def test_active_contract_blocks_metric_drift(monkeypatch, tmp_path):
    _disable_disk(monkeypatch, tmp_path)
    state=_state(); rows=[{"userIdentity.accessKeyId":"A","message_count":"1"},{"userIdentity.accessKeyId":"B","message_count":"5"}]
    q='index=botsv3 | stats dc(errorMessage) AS message_count BY userIdentity.accessKeyId'; ev=_evidence("E4",q,rows); state["evidence"].append(ev); store.ingest_machine_evidence(state,ev,ev["machine_result"])
    c=store.create_measurement_contract(state,requested_quantity="most distinct errors",evidence_id="E4",group_field="userIdentity.accessKeyId",metric_field="message_count",source_expression="errorMessage",support_ids=["E4"],reason="semantic choice")
    bad='index=botsv3 | stats dc(errorCode) AS distinct_count BY userIdentity.accessKeyId'
    good='index=botsv3 | stats dc(errorMessage) AS other_alias BY userIdentity.accessKeyId'
    assert store.measurement_contract_violations(bad,c)
    assert store.measurement_contract_violations(good,c) == []

def test_measurement_resolution_persists_semantics_and_survives_prior_failure(monkeypatch, tmp_path):
    import commander_agent.core.measurement_resolution as mr
    _disable_disk(monkeypatch, tmp_path)
    state = _state()
    rows = [
        {"userIdentity.accessKeyId":"A","code_count":"1","message_count":"1"},
        {"userIdentity.accessKeyId":"B","code_count":"1","message_count":"5"},
        {"userIdentity.accessKeyId":"C","code_count":"1","message_count":"1"},
    ]
    q = 'index=botsv3 | stats dc(errorCode) AS code_count dc(errorMessage) AS message_count BY userIdentity.accessKeyId'
    ev = _evidence("E4", q, rows); state["evidence"].append(ev); store.ingest_machine_evidence(state, ev, ev["machine_result"])
    metrics = {}

    # First call simulates the prior truncation. Facts must remain intact.
    monkeypatch.setattr(mr, "structured_chat", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("MODEL_OUTPUT_TRUNCATED")))
    assert mr.resolve_measurement_semantics(question=state["question"], case_state=state, metrics=metrics) is None
    assert any(o.get("metric_field")=="message_count" and o.get("group_value")=="B" and o.get("value")==5 for o in state["observations"])

    message_measurement = next(c for c in store.measurement_candidates(state) if c["metric_field"] == "message_count")
    payload = {
        "selected_measurement_id": message_measurement["measurement_id"],
        "reason":"Representative events show one shared error code but materially different error messages, so errorCode is too coarse.",
        "confidence":"high",
    }
    monkeypatch.setattr(mr, "structured_chat", lambda **kwargs: {"message":{"content":json.dumps(payload)}})
    contract = mr.resolve_measurement_semantics(question=state["question"], case_state=state, metrics=metrics)
    assert contract and contract["metric_field"] == "message_count"
    assert contract["measurement_id"] == message_measurement["measurement_id"]
    assert contract["support_ids"] == ["E4"]
    assert contract["support_observation_ids"]
    assert store.assessment_from_active_contract(state)["candidate"] == "B"
