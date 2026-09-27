import json
from types import SimpleNamespace

import pytest

from commander_agent import config
from commander_agent.reasoning import broker as broker_module
from commander_agent.reasoning.broker import ReasoningBroker
from commander_agent.skills.result_semantics_recovery.scripts import reviewer as semantic_reviewer
from commander_agent.skills.structured_output_validation.scripts.schema import parse_model_object
from commander_agent.state import validation as validation_module
from commander_agent.state.evidence import (
    candidate_is_quarantined,
    current_evidence_version,
    new_case_state,
    quarantine_candidate,
    record_evidence,
    record_semantic_review,
)
from commander_agent.core import semantic_recovery as semantic_recovery_module


def _fake_response(payload, prompt_tokens=100, output_tokens=50):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(
        message=SimpleNamespace(content=text),
        prompt_eval_count=prompt_tokens,
        eval_count=output_tokens,
    )


def _base_case():
    return {
        "case_id": "case-x",
        "evidence": [],
        "signature_to_evidence": {},
        "timeline_rows": 0,
        "anchor": None,
        "structured_findings": [],
        "semantic_reviews": [],
        "semantic_findings": [],
        "candidate_quarantine": [],
        "semantic_action_signatures": set(),
        "semantic_reviews_by_version": {},
        "evidence_version": 0,
        "failed_query_shapes": set(),
        "fatal_tool_error": None,
        "active_skills": ["result_semantics_recovery"],
    }


def test_openai_is_disabled_by_default_and_path_not_user_specific():
    assert config.ENABLE_OPENAI_REASONING is False
    if config.OPENAI_KEY_FILE is not None:
        text = str(config.OPENAI_KEY_FILE).lower()
        assert "c:\\users\\" not in text


def test_reasoner_preflight_disables_remote_on_failure(monkeypatch):
    b = ReasoningBroker()
    b.enabled = True
    monkeypatch.setattr(broker_module, "read_api_key", lambda: ("dummy", ""))

    def fail(*args, **kwargs):
        raise RuntimeError("test failure")

    monkeypatch.setattr(broker_module, "create_response", fail)
    ok, message = b.preflight()
    assert ok is False
    assert b.available is False
    assert "using local Ollama only" in message


def test_reasoner_preflight_tests_both_models(monkeypatch):
    calls = []
    b = ReasoningBroker()
    b.enabled = True
    monkeypatch.setattr(broker_module, "read_api_key", lambda: ("dummy", ""))

    def success(api_key, model, prompt, effort, max_output_tokens):
        calls.append(model)
        return {"text": "OK", "model": model, "usage": {}}

    monkeypatch.setattr(broker_module, "create_response", success)
    ok, _ = b.preflight()
    assert ok is True
    assert calls == [config.OPENAI_LUNA_MODEL, config.OPENAI_TERRA_MODEL]
    assert b.available is True


def test_semantic_protocol_has_small_mandatory_contract_and_nullable_action_fields():
    payload = {
        "decision": "RUN_ALTERNATIVE_QUERY",
            "capability": "alternative_aggregation",
        "candidate_status": "ambiguous",
        "reason": "Current field is too coarse.",
        "candidate": None,
        "recommended_spl": "index=x | stats count BY entity detail | stats count BY entity",
        "support_ids": ["E1"],
    }
    ok, obj, errors = parse_model_object(json.dumps(payload), semantic_reviewer.SEMANTIC_DECISION_SCHEMA)
    assert ok, errors
    assert obj["candidate"] is None


def test_semantic_skill_uses_ollama_structured_output_schema(monkeypatch):
    payload = {
        "decision": "RUN_CHECK_QUERY",
        "capability": "semantic_field_review",
        "candidate_status": "ambiguous",
        "reason": "Need a focused check.",
        "candidate": None,
        "recommended_spl": "index=x | stats count BY entity code | stats count BY entity",
        "support_ids": [],
    }
    captured = {}

    def fake_chat(**kwargs):
        captured.update(kwargs)
        return _fake_response(payload)

    monkeypatch.setattr(semantic_reviewer, "_ollama_chat", fake_chat)
    case_state = _base_case()
    metrics = {}
    result = semantic_reviewer.review_result_semantics(
        question="Which entity has the most distinct failures?",
        query_strategy={"investigation_type": "aggregation"},
        case_state=case_state,
        metrics=metrics,
    )
    assert result["decision"] == "RUN_CHECK_QUERY"
    assert captured["format"] == semantic_reviewer.SEMANTIC_DECISION_SCHEMA
    assert result["evidence_version"] == 0


def test_semantic_skill_repairs_schema_once_without_new_investigation(monkeypatch):
    invalid = {
        "decision": "REJECT_CANDIDATE",
        "capability": "candidate_validation",
        "candidate_status": "unsupported",
        # missing reason
        "candidate": None,
        "recommended_spl": None,
        "support_ids": ["E1"],
    }
    repaired = {
        "decision": "REJECT_CANDIDATE",
        "capability": "candidate_validation",
        "candidate_status": "unsupported",
        "reason": "Candidate is not supported by the evidence.",
        "candidate": None,
        "recommended_spl": None,
        "support_ids": ["E1"],
    }
    calls = iter([_fake_response(invalid), _fake_response(repaired, 20, 10)])
    monkeypatch.setattr(semantic_reviewer, "_ollama_chat", lambda **kwargs: next(calls))
    case_state = _base_case()
    case_state["evidence"].append({"evidence_id": "E1", "source": "splunk", "query": "q", "result_excerpt": "{}", "method_class": "targeted_rows"})
    case_state["evidence_version"] = 1
    metrics = {}
    result = semantic_reviewer.review_result_semantics(
        question="Which entity?",
        query_strategy={},
        case_state=case_state,
        metrics=metrics,
        candidate="ENTITY-A",
    )
    assert result["decision"] == "REJECT_CANDIDATE"
    assert result["reasoning_source"] == "local_format_repair"
    assert metrics["semantic_format_repairs"] == 1
    assert metrics["semantic_format_repair_successes"] == 1


def test_data_evidence_version_changes_but_semantic_reasoning_does_not(monkeypatch, tmp_path):
    # Avoid writing persistent test ledgers into the package directory.
    import commander_agent.state.evidence as ev
    monkeypatch.setattr(ev, "EVIDENCE_LEDGER_FILE", str(tmp_path / "evidence.jsonl"))
    monkeypatch.setattr(ev, "SEMANTIC_FINDINGS_FILE", str(tmp_path / "semantic.jsonl"))

    state = _base_case()
    record_evidence(state, "splunk", "search_oneshot", '{"event_count":1,"events":[{"x":1}]}', query="q1", signature="s1", method_class="targeted_rows")
    assert current_evidence_version(state) == 1

    review = {
        "decision": "REJECT_CANDIDATE",
        "capability": "candidate_validation",
        "candidate_status": "unsupported",
        "reason": "unsupported",
        "candidate": None,
        "recommended_spl": None,
        "support_ids": ["E1"],
    }
    record_semantic_review(state, review, support_ids=["E1"])
    assert current_evidence_version(state) == 1


def test_candidate_quarantine_expires_only_after_new_data_evidence(monkeypatch, tmp_path):
    import commander_agent.state.evidence as ev
    monkeypatch.setattr(ev, "EVIDENCE_LEDGER_FILE", str(tmp_path / "evidence.jsonl"))

    state = _base_case()
    state["evidence_version"] = 2
    quarantine_candidate(state, "ENTITY-A is the answer", {"reason": "conflict"})
    assert candidate_is_quarantined(state, "ENTITY-A is the answer") is True

    record_evidence(state, "splunk", "search_oneshot", '{"event_count":1,"events":[{"x":1}]}', query="q2", signature="s2", method_class="targeted_rows")
    assert current_evidence_version(state) == 3
    assert candidate_is_quarantined(state, "ENTITY-A is the answer") is False


def test_semantic_validation_accepts_authoritative_supported_gate():
    case_state = _base_case()
    case_state["evidence"] = [
        {"evidence_id": "E1", "source": "splunk", "method_class": "aggregation_distinct", "independence_key": "a", "event_count": 1, "query": "q1", "result_excerpt": "{}", "quality": 0.95},
        {"evidence_id": "E2", "source": "splunk", "method_class": "aggregation_other", "independence_key": "b", "event_count": 1, "query": "q2", "result_excerpt": "{}", "quality": 0.95},
    ]
    plan = {"strategy": {"investigation_type": "aggregation"}, "query_strategy": {}}
    metrics = {"validation_reviews": 0, "semantic_validation_blocks": 0}
    gate = {
        "decision": "ACCEPT",
            "capability": "candidate_validation",
        "candidate_status": "supported",
        "reason": "Two evidence paths agree.",
        "candidate": "ENTITY-B",
        "recommended_spl": None,
        "support_ids": ["E1", "E2"],
        "confidence": "high",
    }
    result = validation_module.validation_review(
        "Which entity has the most distinct failures?",
        "ENTITY-B",
        plan,
        case_state,
        metrics,
        semantic_gate=gate,
    )
    assert result["decision"] == "accept"
    assert result["support_ids"] == ["E1", "E2"]


def test_semantic_validation_fails_closed_without_gate():
    case_state = _base_case()
    plan = {"strategy": {"investigation_type": "aggregation"}, "query_strategy": {}}
    metrics = {"validation_reviews": 0, "semantic_validation_blocks": 0}
    result = validation_module.validation_review(
        "Which entity?",
        "ENTITY-A",
        plan,
        case_state,
        metrics,
        semantic_gate=None,
    )
    assert result["decision"] == "continue"
    assert metrics["semantic_validation_blocks"] == 1


@pytest.mark.asyncio
async def test_semantic_skill_executes_followup_query_then_rechecks(monkeypatch, tmp_path):
    import commander_agent.state.evidence as ev
    monkeypatch.setattr(ev, "EVIDENCE_LEDGER_FILE", str(tmp_path / "evidence.jsonl"))
    monkeypatch.setattr(ev, "SEMANTIC_FINDINGS_FILE", str(tmp_path / "semantic.jsonl"))

    reviews = []
    def fake_review_capability(**kwargs):
        reviews.append(kwargs["capability"])
        assert kwargs["capability"] == "alternative_aggregation"
        return {
            "decision": "RUN_ALTERNATIVE_QUERY",
            "capability": "alternative_aggregation",
            "candidate_status": "not_evaluated",
            "reason": "Use a materially different same-scope calculation.",
            "candidate": None,
            "recommended_spl": "index=x source=y | stats count BY entity detail | stats count AS distinct_detail BY entity",
            "support_ids": ["E1"],
            "suggested_capabilities": [],
            "scope_change": {"requested": False, "reason": None},
        }
    monkeypatch.setattr(semantic_recovery_module, "review_capability", fake_review_capability)

    assessments = iter([
        {"status":"AMBIGUOUS","candidate":None,"support_ids":["E1"],"reason":"Initial evidence is ambiguous.","requested_quantity":"distinct detail","confidence":"medium","evidence_version":1},
        {"status":"SUPPORTED","candidate":"ENTITY-B","support_ids":["E1","E2"],"reason":"New machine evidence supports one candidate.","requested_quantity":"distinct detail","confidence":"high","evidence_version":2},
    ])
    monkeypatch.setattr(semantic_recovery_module, "assess_candidate", lambda **kwargs: next(assessments))

    executed = []
    async def fake_execute(**kwargs):
        executed.append(kwargs["arguments"]["query"])
        state = kwargs["case_state"]
        state["evidence_version"] += 1
        evidence = {
            "evidence_id": "E2", "source": "splunk", "method_class": "aggregation_other",
            "query": kwargs["arguments"]["query"], "result_excerpt": '{"entity":"ENTITY-B","distinct_detail":"2"}',
            "evidence_version": state["evidence_version"],
        }
        state["evidence"].append(evidence)
        return "{}", evidence, kwargs["arguments"]
    monkeypatch.setattr(semantic_recovery_module, "execute_splunk_search", fake_execute)
    monkeypatch.setattr(semantic_recovery_module, "record_semantic_review", lambda *a, **k: None)
    monkeypatch.setattr(semantic_recovery_module, "record_semantic_finding", lambda *a, **k: {"finding_id": "S1"})
    monkeypatch.setattr(semantic_recovery_module, "record_semantic_finding_status", lambda *a, **k: None)

    def fake_supervise(**kwargs):
        result = kwargs.get("last_result") or {}
        if result.get("status") == "SUPPORTED":
            return {"evaluation":"PASS","goal_satisfied":True,"remaining_gap":"","next_capabilities":[],"irrelevant_capabilities":[],"invalidate_from_stage":None,"allow_validation":True,"allow_stop":False,"reason":"Candidate may proceed."}
        if (result.get("candidate_assessment") or {}).get("status") == "SUPPORTED":
            return {"evaluation":"PASS","goal_satisfied":True,"remaining_gap":"","next_capabilities":[],"irrelevant_capabilities":[],"invalidate_from_stage":None,"allow_validation":True,"allow_stop":False,"reason":"Candidate may proceed."}
        return {"evaluation":"PARTIAL","goal_satisfied":False,"remaining_gap":"Need independent evidence.","next_capabilities":["alternative_aggregation"],"irrelevant_capabilities":[],"invalidate_from_stage":None,"allow_validation":False,"allow_stop":False,"reason":"Continue."}
    monkeypatch.setattr(semantic_recovery_module, "_supervise", fake_supervise)

    state = _base_case()
    state["scope_contract"] = {"base_scope_spl":"index=x source=y","required_head_atoms":["index=x","source=y"],"required_filter_stages":[]}
    state["evidence"] = [{"evidence_id":"E1","source":"splunk","method_class":"aggregation_distinct","query":"index=x source=y | stats dc(detail) BY entity","result_excerpt":"{}"}]
    state["evidence_version"] = 1
    metrics = {}
    result = await semantic_recovery_module.run_semantic_recovery(
        session=object(), question="Which entity has the most distinct details?", query_strategy={},
        case_state=state, metrics=metrics, active_skills=["result_semantics_recovery"],
    )
    assert result["decision"] == "ACCEPT"
    assert result["candidate"] == "ENTITY-B"
    assert reviews == ["alternative_aggregation"]
    assert len(executed) == 1
    assert metrics["semantic_skill_accepts"] == 1



@pytest.mark.asyncio
async def test_semantic_recovery_exhausts_stalled_path_and_allows_supervised_stop(monkeypatch, tmp_path):
    import commander_agent.state.evidence as ev
    from commander_agent.state.orchestration import RECOVERY_CAPABILITIES
    monkeypatch.setattr(ev, "SEMANTIC_FINDINGS_FILE", str(tmp_path / "semantic.jsonl"))

    monkeypatch.setattr(semantic_recovery_module, "assess_candidate", lambda **kwargs: {
        "status":"AMBIGUOUS","candidate":None,"support_ids":["E1"],"reason":"Still ambiguous.",
        "requested_quantity":"value","confidence":"medium","evidence_version":1,
    })
    monkeypatch.setattr(semantic_recovery_module, "review_capability", lambda **kwargs: {
        "decision":"DEFER_CAPABILITY","capability":kwargs["capability"],"candidate_status":"not_evaluated",
        "reason":"This route cannot resolve the gap.","candidate":None,"recommended_spl":None,
        "support_ids":["E1"],"suggested_capabilities":[],"scope_change":{"requested":False,"reason":None},
    })
    monkeypatch.setattr(semantic_recovery_module, "record_semantic_review", lambda *a, **k: None)
    monkeypatch.setattr(semantic_recovery_module, "record_semantic_finding", lambda *a, **k: {"finding_id": "S1"})
    monkeypatch.setattr(semantic_recovery_module, "record_semantic_finding_status", lambda *a, **k: None)

    def fake_supervise(**kwargs):
        state = kwargs["case_state"]
        action = kwargs.get("last_action") or {}
        if action.get("decision") == "CANDIDATE_ASSESSMENT":
            return {"evaluation":"PARTIAL","goal_satisfied":False,"remaining_gap":"Unresolved","next_capabilities":["semantic_field_review"],"irrelevant_capabilities":[],"invalidate_from_stage":None,"allow_validation":False,"allow_stop":False,"reason":"Try one route."}
        orch = state.setdefault("orchestration", {})
        orch.setdefault("irrelevant_by_version", {})[str(state.get("evidence_version", 0))] = list(RECOVERY_CAPABILITIES)
        return {"evaluation":"BLOCKED","goal_satisfied":False,"remaining_gap":"Unresolved","next_capabilities":[],"irrelevant_capabilities":list(RECOVERY_CAPABILITIES),"invalidate_from_stage":None,"allow_validation":False,"allow_stop":True,"reason":"All applicable paths exhausted."}
    monkeypatch.setattr(semantic_recovery_module, "_supervise", fake_supervise)

    state = _base_case()
    state["evidence"] = [{"evidence_id":"E1","source":"splunk","method_class":"targeted_rows","query":"index=x","result_excerpt":"{}"}]
    state["evidence_version"] = 1
    metrics = {}
    result = await semantic_recovery_module.run_semantic_recovery(
        session=object(), question="Question", query_strategy={}, case_state=state,
        metrics=metrics, active_skills=["result_semantics_recovery"],
    )
    assert result["decision"] == "STOP_UNRESOLVED"
    assert result["orchestration_status"] == "semantic_unresolved_routes_exhausted"
    assert metrics["capability_deferrals"] == 1



def test_action_contract_rejects_candidate_less_rejection():
    obj = {
        "decision": "REJECT_CANDIDATE",
        "capability": "candidate_validation",
        "candidate_status": "ambiguous",
        "reason": "Evidence is tied.",
        "candidate": None,
        "recommended_spl": None,
        "support_ids": ["E1", "E2"],
    }
    errors = semantic_reviewer.validate_action_contract(obj, current_candidate="")
    assert any("CURRENT CANDIDATE" in e for e in errors)
    assert any("candidate_status=unsupported" in e for e in errors)


def test_action_contract_allows_unresolved_without_candidate():
    obj = {
        "decision": "STOP_UNRESOLVED",
        "capability": "stop",
        "candidate_status": "ambiguous",
        "reason": "No useful focused evidence action remains.",
        "candidate": None,
        "recommended_spl": None,
        "support_ids": ["E1"],
    }
    assert semantic_reviewer.validate_action_contract(obj, current_candidate="") == []


def test_semantic_skill_repairs_inconsistent_action_to_evidence_query(monkeypatch):
    initial = {
        "decision": "REJECT_CANDIDATE",
        "capability": "candidate_validation",
        "candidate_status": "ambiguous",
        "reason": "Current evidence is tied.",
        "candidate": None,
        "recommended_spl": None,
        "support_ids": ["E1", "E2"],
    }
    repaired = {
        "decision": "INVESTIGATE_SCHEMA",
        "capability": "schema_inspection",
        "candidate_status": "ambiguous",
        "reason": "The current representation may be too coarse, so inspect alternate detail fields.",
        "candidate": None,
        "recommended_spl": "index=x | table entity code detail | head 20",
        "support_ids": ["E1", "E2"],
    }
    calls = iter([_fake_response(initial), _fake_response(repaired, 20, 10)])
    monkeypatch.setattr(semantic_reviewer, "_ollama_chat", lambda **kwargs: next(calls))
    state = _base_case()
    state["evidence"] = [
        {"evidence_id": "E1", "source": "splunk", "query": "q1", "result_excerpt": "{}", "method_class": "aggregation_distinct"},
        {"evidence_id": "E2", "source": "splunk", "query": "q2", "result_excerpt": "{}", "method_class": "aggregation_values"},
    ]
    state["evidence_version"] = 2
    metrics = {}
    result = semantic_reviewer.review_result_semantics(
        question="Which entity has the most distinct failures?",
        query_strategy={"investigation_type": "aggregation"},
        case_state=state,
        metrics=metrics,
        candidate="",
    )
    assert result["decision"] == "INVESTIGATE_SCHEMA"
    assert result["recommended_spl"]
    assert result["reasoning_source"] == "local_action_repair"
    assert metrics["semantic_action_repairs"] == 1
    assert metrics["semantic_action_repair_successes"] == 1
    assert metrics["semantic_action_contract_rejections"] >= 1


@pytest.mark.asyncio
async def test_child_contract_failure_returns_to_supervisor_and_switches_capability(monkeypatch, tmp_path):
    import commander_agent.state.evidence as ev
    monkeypatch.setattr(ev, "SEMANTIC_FINDINGS_FILE", str(tmp_path / "semantic.jsonl"))

    calls = []
    def fake_review(**kwargs):
        cap = kwargs["capability"]
        calls.append(cap)
        if cap == "semantic_field_review":
            return None
        if cap == "schema_inspection":
            return {
                "decision":"INVESTIGATE_SCHEMA","capability":"schema_inspection","candidate_status":"not_evaluated",
                "reason":"Inspect detail fields.","candidate":None,
                "recommended_spl":"index=x source=y | table actor code detail | head 20",
                "support_ids":["E1"],"suggested_capabilities":[],"scope_change":{"requested":False,"reason":None},
            }
        raise AssertionError(cap)
    monkeypatch.setattr(semantic_recovery_module, "review_capability", fake_review)

    assessments = iter([
        {"status":"AMBIGUOUS","candidate":None,"support_ids":["E1"],"reason":"Initial ambiguity.","requested_quantity":"detail","confidence":"medium","evidence_version":1},
        {"status":"SUPPORTED","candidate":"ENTITY-B","support_ids":["E1","E2"],"reason":"Schema evidence supports candidate.","requested_quantity":"detail","confidence":"high","evidence_version":2},
    ])
    monkeypatch.setattr(semantic_recovery_module, "assess_candidate", lambda **kwargs: next(assessments))
    monkeypatch.setattr(semantic_recovery_module, "record_semantic_review", lambda *a, **k: None)
    monkeypatch.setattr(semantic_recovery_module, "record_semantic_finding", lambda *a, **k: {"finding_id": "S1"})
    monkeypatch.setattr(semantic_recovery_module, "record_semantic_finding_status", lambda *a, **k: None)

    async def fake_execute(**kwargs):
        state = kwargs["case_state"]
        state["evidence_version"] += 1
        evidence = {"evidence_id":"E2","source":"splunk","method_class":"schema_rows","query":kwargs["arguments"]["query"],"result_excerpt":"{}","evidence_version":state["evidence_version"]}
        state["evidence"].append(evidence)
        return "{}", evidence, kwargs["arguments"]
    monkeypatch.setattr(semantic_recovery_module, "execute_splunk_search", fake_execute)

    def fake_supervise(**kwargs):
        result = kwargs.get("last_result") or {}
        action = kwargs.get("last_action") or {}
        if action.get("decision") == "CANDIDATE_ASSESSMENT":
            return {"evaluation":"PARTIAL","goal_satisfied":False,"remaining_gap":"Field semantics unresolved","next_capabilities":["semantic_field_review"],"irrelevant_capabilities":[],"invalidate_from_stage":None,"allow_validation":False,"allow_stop":False,"reason":"Start field review."}
        if result.get("status") == "investigator_contract_failure":
            return {"evaluation":"FAIL","goal_satisfied":False,"remaining_gap":"Field semantics unresolved","next_capabilities":["schema_inspection"],"irrelevant_capabilities":[],"invalidate_from_stage":None,"allow_validation":False,"allow_stop":False,"reason":"Switch to schema inspection."}
        if (result.get("candidate_assessment") or {}).get("status") == "SUPPORTED":
            return {"evaluation":"PASS","goal_satisfied":True,"remaining_gap":"","next_capabilities":[],"irrelevant_capabilities":[],"invalidate_from_stage":None,"allow_validation":True,"allow_stop":False,"reason":"Proceed to validation."}
        raise AssertionError((action, result))
    monkeypatch.setattr(semantic_recovery_module, "_supervise", fake_supervise)

    state = _base_case()
    state["scope_contract"] = {"base_scope_spl":"index=x source=y","required_head_atoms":["index=x","source=y"],"required_filter_stages":[]}
    state["evidence"] = [{"evidence_id":"E1","source":"splunk","method_class":"aggregation_distinct","query":"index=x source=y | stats dc(code) BY actor","result_excerpt":"{}"}]
    state["evidence_version"] = 1
    metrics = {}
    result = await semantic_recovery_module.run_semantic_recovery(
        session=object(), question="Which entity?", query_strategy={}, case_state=state,
        metrics=metrics, active_skills=["result_semantics_recovery"],
    )
    assert result["decision"] == "ACCEPT"
    assert result["candidate"] == "ENTITY-B"
    assert calls[:2] == ["semantic_field_review", "schema_inspection"]
    assert metrics["investigator_contract_failures"] == 1
    assert metrics["capability_failovers"] >= 1

