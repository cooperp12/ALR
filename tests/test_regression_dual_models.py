import json
from pathlib import Path

from commander_agent import config
from commander_agent.reasoning import local_ollama
from commander_agent.skills.result_semantics_recovery.scripts import capabilities
from commander_agent.skills.investigation_supervisor.scripts import reviewer as supervisor_reviewer


def test_role_model_defaults_match_current_design():
    assert config.INVESTIGATOR_MODEL == "gpt-oss:20b"
    assert config.SUPERVISOR_MODEL == "granite4.2:8b"
    assert config.MODEL == config.INVESTIGATOR_MODEL
    assert config.INVESTIGATOR_NUM_CTX == 8192
    assert config.SUPERVISOR_NUM_CTX == 8192
    assert config.OLLAMA_ROLE_KEEP_ALIVE == "0s"


def test_settings_ship_explicit_dual_model_roles():
    settings = json.loads(Path("config/settings.json").read_text(encoding="utf-8"))
    assert settings["ollama_investigator_model"] == "gpt-oss:20b"
    assert settings["ollama_supervisor_model"] == "granite4.2:8b"
    assert settings["ollama_investigator_think"] == "low"
    assert settings["ollama_supervisor_think"] is False
    assert settings["ollama_role_keep_alive"] == "0s"


def test_supervisor_structured_call_uses_granite_without_thinking(monkeypatch):
    calls = []

    def fake_chat(**kwargs):
        calls.append(kwargs)
        return {
            "message": {"content": '{"status":"OK"}'},
            "prompt_eval_count": 10,
            "eval_count": 7,
            "done_reason": "stop",
        }

    monkeypatch.setattr(local_ollama, "_ollama_chat", fake_chat)
    metrics = {}
    schema = {"type": "object", "required": ["status"], "properties": {"status": {"type": "string"}}}
    response = local_ollama.structured_chat(
        role="supervisor",
        messages=[{"role": "user", "content": "x"}],
        schema=schema,
        metrics=metrics,
        purpose="test",
    )
    assert local_ollama.message_content(response) == '{"status":"OK"}'
    assert calls[0]["model"] == "granite4.2:8b"
    assert calls[0]["think"] is False
    assert calls[0]["keep_alive"] == "0s"
    assert calls[0]["options"]["num_ctx"] == 8192
    assert metrics["ollama_supervisor_output_tokens"] == 7


def test_investigator_truncation_retries_with_supported_low_thinking(monkeypatch):
    calls = []
    responses = iter([
        {
            "message": {"content": "", "thinking": "reasoning that hit the output ceiling"},
            "prompt_eval_count": 20,
            "eval_count": config.INVESTIGATOR_STRUCTURED_NUM_PREDICT,
            "done_reason": "length",
        },
        {
            "message": {"content": '{"status":"OK"}', "thinking": ""},
            "prompt_eval_count": 20,
            "eval_count": 8,
            "done_reason": "stop",
        },
    ])

    def fake_chat(**kwargs):
        calls.append(kwargs)
        return next(responses)

    monkeypatch.setattr(local_ollama, "_ollama_chat", fake_chat)
    metrics = {}
    schema = {"type": "object", "required": ["status"], "properties": {"status": {"type": "string"}}}
    response = local_ollama.structured_chat(
        role="investigator",
        messages=[{"role": "user", "content": "x"}],
        schema=schema,
        metrics=metrics,
        purpose="capability_test",
    )
    assert local_ollama.message_content(response) == '{"status":"OK"}'
    assert len(calls) == 2
    assert calls[0]["model"] == "gpt-oss:20b"
    assert calls[0]["think"] == "low"
    assert calls[1]["think"] == "low"
    assert calls[1]["options"]["num_predict"] == config.INVESTIGATOR_RETRY_NUM_PREDICT
    assert metrics["ollama_truncation_detected"] == 1
    assert metrics["ollama_truncation_retries"] == 1


def _case_state():
    return {
        "case_id": "dual-model-test",
        "evidence": [],
        "evidence_version": 0,
        "scope_contract": {"base_scope_spl": "index=x"},
    }


def test_capability_reviews_are_assigned_to_investigator_role(monkeypatch):
    seen = {}

    def fake_structured_chat(**kwargs):
        seen.update(kwargs)
        return {
            "message": {"content": json.dumps({
                "verdict": "UNKNOWN",
                "reason": "Need schema evidence.",
                "support_ids": [],
                "suggested_capabilities": ["schema_inspection"],
            })},
            "prompt_eval_count": 1,
            "eval_count": 1,
            "done_reason": "stop",
        }

    monkeypatch.setattr(capabilities, "structured_chat", fake_structured_chat)
    result = capabilities.review_capability(
        capability="semantic_field_review",
        question="Which actor has the most distinct failures?",
        case_state=_case_state(),
        metrics={},
        supervisor_feedback={"remaining_gap": "Resolve semantics."},
    )
    assert seen["role"] == "investigator"
    assert result["decision"] == "SEMANTIC_FINDING"


def test_candidate_assessment_uses_supervisor_role(monkeypatch):
    seen = {}

    def fake_structured_chat(**kwargs):
        seen.update(kwargs)
        return {
            "message": {"content": json.dumps({
                "status": "AMBIGUOUS",
                "candidate": None,
                "support_ids": [],
                "reason": "Insufficient evidence.",
                "requested_quantity": "distinct failures",
                "confidence": "medium",
            })},
            "prompt_eval_count": 1,
            "eval_count": 1,
            "done_reason": "stop",
        }

    monkeypatch.setattr(capabilities, "structured_chat", fake_structured_chat)
    result = capabilities.assess_candidate(
        question="Which actor has the most distinct failures?",
        case_state=_case_state(),
        metrics={},
    )
    assert seen["role"] == "supervisor"
    assert seen["think"] is False
    assert result["status"] == "AMBIGUOUS"


def test_supervisor_review_uses_supervisor_role(monkeypatch):
    seen = {}

    def fake_structured_chat(**kwargs):
        seen.update(kwargs)
        return {
            "message": {"content": json.dumps({
                "evaluation": "PARTIAL",
                "goal_satisfied": False,
                "remaining_gap": "Need more evidence.",
                "next_capabilities": ["semantic_field_review"],
                "irrelevant_capabilities": [],
                "invalidate_from_stage": None,
                "allow_validation": False,
                "allow_stop": False,
                "reason": "Continue.",
            })},
            "prompt_eval_count": 1,
            "eval_count": 1,
            "done_reason": "stop",
        }

    state = _case_state()
    state["signature_to_evidence"] = {}
    state["semantic_reviews"] = []
    state["semantic_findings"] = []
    state["candidate_quarantine"] = []
    state["semantic_action_signatures"] = set()
    state["semantic_reviews_by_version"] = {}
    state["active_skills"] = []
    monkeypatch.setattr(supervisor_reviewer, "structured_chat", fake_structured_chat)
    result = supervisor_reviewer.review_investigation_progress(
        question="Which actor has the most distinct failures?",
        case_state=state,
        metrics={},
    )
    assert seen["role"] == "supervisor"
    assert seen["think"] is False
    assert result["allow_stop"] is False
