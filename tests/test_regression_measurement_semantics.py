import json

import commander_agent.core.measurement_resolution as mr
import commander_agent.state.evidence_store as store


def _state():
    return {
        "case_id": "current-q3",
        "question": "What IAM user access key generates the most distinct errors when attempting to access IAM resources?",
        "evidence_version": 4,
        "evidence": [],
    }


def _disable_disk(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "OBSERVATION_LEDGER_FILE", tmp_path / "obs.jsonl")
    monkeypatch.setattr(store, "MEASUREMENT_CONTRACT_LEDGER_FILE", tmp_path / "contracts.jsonl")
    monkeypatch.setattr(store, "CANDIDATE_DERIVATION_LEDGER_FILE", tmp_path / "candidates.jsonl")
    monkeypatch.setattr(store, "CASE_STATE_DB_FILE", tmp_path / "state.sqlite")


def _e4(state):
    rows = [
        {"userIdentity.accessKeyId":"A","code_count":"1","message_count":"1","operation_error_count":"1"},
        {"userIdentity.accessKeyId":"B","code_count":"1","message_count":"5","operation_error_count":"5"},
        {"userIdentity.accessKeyId":"C","code_count":"1","message_count":"1","operation_error_count":"1"},
    ]
    query = ('index=botsv3 | eval operation_error=json_object("operation",eventName,"code",errorCode,"message",errorMessage) '
             '| stats dc(errorCode) AS code_count dc(errorMessage) AS message_count '
             'dc(operation_error) AS operation_error_count BY userIdentity.accessKeyId')
    ev={"evidence_id":"E4","source":"splunk","query":query,
        "machine_result":{"events":rows,"rows_omitted":0},"result_excerpt":json.dumps({"events":rows}),"result_limit":50}
    state["evidence"].append(ev)
    store.ingest_machine_evidence(state,ev,ev["machine_result"])
    return ev


def test_complete_measurements_receive_stable_ids(monkeypatch, tmp_path):
    _disable_disk(monkeypatch,tmp_path)
    state=_state(); ev=_e4(state)
    first=store.measurement_candidates(state)
    assert len(first)==3
    assert all(c["measurement_id"].startswith("MEAS-E4-") for c in first)
    ids={c["metric_field"]:c["measurement_id"] for c in first}
    # Duplicate ingestion must neither duplicate nor renumber measurements.
    store.ingest_machine_evidence(state,ev,ev["machine_result"])
    second=store.measurement_candidates(state)
    assert {c["metric_field"]:c["measurement_id"] for c in second}==ids


def test_semantic_model_selects_only_id_python_reconstructs_contract(monkeypatch, tmp_path):
    _disable_disk(monkeypatch,tmp_path)
    state=_state(); _e4(state)
    op=next(c for c in store.measurement_candidates(state) if c["metric_field"]=="operation_error_count")
    model={"selected_measurement_id":op["measurement_id"],
           "reason":"One generic AccessDenied code spans materially different IAM operations; the operation/error representation preserves those distinct failures.",
           "confidence":"high"}
    monkeypatch.setattr(mr,"structured_chat",lambda **kwargs:{"message":{"content":json.dumps(model)}})
    metrics={}
    contract=mr.resolve_measurement_semantics(question=state["question"],case_state=state,metrics=metrics)
    assert contract["measurement_id"]==op["measurement_id"]
    assert contract["evidence_id"]=="E4"
    assert contract["group_field"]=="userIdentity.accessKeyId"
    assert contract["metric_field"]=="operation_error_count"
    assert contract["source_expression"]=="operation_error"
    assert contract["support_ids"]==["E4"]
    assert contract["support_observation_ids"]==op["observation_ids"]
    assessment=store.assessment_from_active_contract(state)
    assert assessment["status"]=="SUPPORTED"
    assert assessment["candidate"]=="B"
    assert assessment["maximum_value"]==5


def test_semantic_output_cannot_supply_provenance_or_fields(monkeypatch, tmp_path):
    _disable_disk(monkeypatch,tmp_path)
    state=_state(); _e4(state)
    op=next(c for c in store.measurement_candidates(state) if c["metric_field"]=="operation_error_count")
    # Extra bookkeeping fields violate the tiny skill contract.
    model={"selected_measurement_id":op["measurement_id"],"reason":"valid semantics","confidence":"high","support_ids":["E999"]}
    monkeypatch.setattr(mr,"structured_chat",lambda **kwargs:{"message":{"content":json.dumps(model)}})
    metrics={}
    assert mr.resolve_measurement_semantics(question=state["question"],case_state=state,metrics=metrics) is None
    assert not state.get("measurement_contracts")
    assert metrics["measurement_resolution_failures"]==1


def test_unknown_measurement_id_fails_closed(monkeypatch, tmp_path):
    _disable_disk(monkeypatch,tmp_path)
    state=_state(); _e4(state)
    model={"selected_measurement_id":"MEAS-E4-does-not-exist","reason":"guess","confidence":"low"}
    monkeypatch.setattr(mr,"structured_chat",lambda **kwargs:{"message":{"content":json.dumps(model)}})
    metrics={}
    assert mr.resolve_measurement_semantics(question=state["question"],case_state=state,metrics=metrics) is None
    assert store.active_measurement_contract(state) is None


def test_null_selection_is_ambiguity_not_failure(monkeypatch, tmp_path):
    _disable_disk(monkeypatch,tmp_path)
    state=_state(); _e4(state)
    model={"selected_measurement_id":None,"reason":"errorMessage and operation_error are equally defensible from current evidence","confidence":"low"}
    monkeypatch.setattr(mr,"structured_chat",lambda **kwargs:{"message":{"content":json.dumps(model)}})
    metrics={}
    assert mr.resolve_measurement_semantics(question=state["question"],case_state=state,metrics=metrics) is None
    assert metrics["measurement_resolution_unresolved"]==1
    assert state["measurement_resolution_last"]["status"]=="AMBIGUOUS"

def test_grouped_pair_verification_retains_operation_error_definition():
    from commander_agent.core.reference_investigation import grouped_check_query
    record={"query":('index=botsv3 sourcetype=aws:cloudtrail | eval operation_error=json_object("operation",eventName,"code",errorCode,"message",errorMessage) '
                     '| stats dc(errorCode) AS code_count dc(errorMessage) AS message_count dc(operation_error) AS operation_error_count BY userIdentity.accessKeyId')}
    q=grouped_check_query(record,"userIdentity.accessKeyId","operation_error_count")
    assert q is not None
    assert 'eval operation_error=json_object' in q
    assert 'stats count BY userIdentity.accessKeyId operation_error' in q
    assert 'checked_count' in q
