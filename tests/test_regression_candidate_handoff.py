import json
import commander_agent.state.evidence_store as store
import commander_agent.skills.result_semantics_recovery.scripts.capabilities as caps


def test_candidate_assessor_uses_contract_without_calling_model(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "OBSERVATION_LEDGER_FILE", tmp_path / "obs.jsonl")
    monkeypatch.setattr(store, "MEASUREMENT_CONTRACT_LEDGER_FILE", tmp_path / "contracts.jsonl")
    monkeypatch.setattr(store, "CANDIDATE_DERIVATION_LEDGER_FILE", tmp_path / "cand.jsonl")
    monkeypatch.setattr(store, "CASE_STATE_DB_FILE", tmp_path / "state.sqlite")
    state={"case_id":"handoff","question":"What key has the most distinct errors?","evidence_version":1,"evidence":[]}
    rows=[{"key":"A","message_count":"1"},{"key":"B","message_count":"5"}]
    q='index=x | stats dc(errorMessage) AS message_count BY key'
    ev={"evidence_id":"E1","source":"splunk","query":q,"machine_result":{"events":rows,"rows_omitted":0},"result_excerpt":json.dumps({"events":rows}),"result_limit":50}
    state["evidence"].append(ev); store.ingest_machine_evidence(state,ev,ev["machine_result"])
    store.create_measurement_contract(state,requested_quantity="most distinct errors",evidence_id="E1",group_field="key",metric_field="message_count",source_expression="errorMessage",support_ids=["E1"],reason="message semantics",confidence="high")
    def boom(*a,**k): raise AssertionError("model should not be called after contract")
    monkeypatch.setattr(caps,"structured_chat",boom)
    metrics={}
    result=caps.assess_candidate(question=state["question"],case_state=state,metrics=metrics)
    assert result["status"]=="SUPPORTED" and result["candidate"]=="B"
    assert result["reasoning_source"]=="deterministic_measurement_contract"
    assert metrics["deterministic_candidate_derivations"]==1
