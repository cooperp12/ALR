import asyncio
import json

from commander_agent.core.executor import execute_splunk_search
from commander_agent.semantic.frame import build_investigation_frame, heuristic_frame
from commander_agent.semantic.binding import resolve_semantic_bindings
from commander_agent.semantic.query_policy import semantic_query_policy_violations
from commander_agent.state.semantic_state import record_semantic_bindings
from commander_agent.skills.strategy import compose_query_strategy
import commander_agent.core.executor as executor_module
import commander_agent.semantic.frame as frame_module

Q3 = "What IAM user access key generates the most distinct errors when attempting to access IAM resources?"


class NeverCalledSession:
    def __init__(self):
        self.called = False

    async def call_tool(self, *args, **kwargs):
        self.called = True
        raise AssertionError("semantic policy should block before Splunk execution")


def _state():
    frame = heuristic_frame(Q3)
    st = {
        "case_id": "current-q3",
        "question": Q3,
        "evidence": [],
        "evidence_version": 0,
        "signature_to_evidence": {},
        "failed_query_shapes": set(),
        "scope_contract": {},
        "investigation_frame": frame,
        "semantic_bindings": [],
        "semantic_conflicts": [],
        "semantic_relationships": [],
        "measurement_contracts": [],
        "active_measurement_contract": None,
    }
    record_semantic_bindings(st, resolve_semantic_bindings(frame, Q3))
    return st


def _metrics():
    return {
        "search_attempts": 0, "queries": [], "guardrail_blocks": 0,
        "scope_guard_blocks": 0, "structured_output_rejections": 0,
        "search_calls": 0, "skill_contract_blocks": 0,
        "retry_diversity_blocks": 0, "current_run_cache_hits": 0,
        "query_cache_hits": 0, "tool_errors": 0, "zero_results": 0,
        "fatal_tool_errors": 0, "semantic_binding_query_blocks": 0,
        "measurement_drift_blocks": 0, "semantic_query_policy_blocks": 0,
        "multivalue_cardinality_blocks": 0, "ranking_population_blocks": 0,
        "supervisor_query_reviews": 0, "supervisor_relationship_review_failures": 0,
    }


def _fake_supervisor(**kwargs):
    return {
        "evaluation": "CONFLICT",
        "goal_satisfied": False,
        "remaining_gap": "Repair the query so it matches the active semantic binding.",
        "next_capabilities": ["semantic_field_review"],
        "irrelevant_capabilities": [],
        "invalidate_from_stage": None,
        "allow_validation": False,
        "allow_stop": False,
        "reason": "The proposed query contradicts an active semantic relationship.",
    }


def test_q3_errorcode_query_is_blocked_before_splunk_and_supervisor_reviews(monkeypatch):
    st = _state(); metrics = _metrics(); session = NeverCalledSession()
    monkeypatch.setattr(executor_module, "review_investigation_progress", _fake_supervisor)
    bad = "index=botsv3 sourcetype=aws:cloudtrail eventSource=iam.amazonaws.com errorCode=* | stats dc(errorCode) as distinct_errors by userIdentity.accessKeyId | sort - distinct_errors | head 1"
    text, evidence, _ = asyncio.run(execute_splunk_search(
        session=session, tool_name="search_oneshot",
        arguments={"query": bad, "output_format": "json", "max_count": 10},
        case_state=st, metrics=metrics, round_number=1,
        active_skills=["aws_cloudtrail", "spl_distinct_count"], phase="DO", question=Q3,
    ))
    payload = json.loads(text)
    assert evidence is None
    assert session.called is False
    assert any(v["code"] == "SEMANTIC_BINDING_QUERY_MISMATCH" for v in payload["violations"])
    assert any(v["code"] == "INCOMPLETE_RANKING_POPULATION" for v in payload["violations"])
    assert payload["supervisor_review"]["evaluation"] == "CONFLICT"
    assert metrics["semantic_binding_query_blocks"] == 1
    assert metrics["supervisor_query_reviews"] == 1


def test_values_then_len_is_blocked_as_cardinality_error():
    st = _state()
    q = "index=botsv3 sourcetype=aws:cloudtrail eventSource=iam.amazonaws.com errorCode=* | stats values(errorMessage) as errors by userIdentity.accessKeyId | eval distinct_errors=len(errors) | sort - distinct_errors"
    violations = semantic_query_policy_violations(q, st, phase="CHECK")
    assert any(v["code"] == "MULTIVALUE_LEN_CARDINALITY_ERROR" for v in violations)


def test_values_then_mvcount_is_allowed_when_binding_matches():
    st = _state()
    q = "index=botsv3 sourcetype=aws:cloudtrail eventSource=iam.amazonaws.com errorCode=* | stats values(errorMessage) as errors by userIdentity.accessKeyId | eval distinct_errors=mvcount(errors) | sort - distinct_errors"
    assert semantic_query_policy_violations(q, st, phase="CHECK") == []


def test_precontract_head1_is_blocked_even_with_correct_measurement():
    st = _state()
    q = "index=botsv3 sourcetype=aws:cloudtrail eventSource=iam.amazonaws.com errorCode=* | stats dc(errorMessage) as distinct_errors by userIdentity.accessKeyId | sort - distinct_errors | head 1"
    violations = semantic_query_policy_violations(q, st, phase="DO")
    assert any(v["code"] == "INCOMPLETE_RANKING_POPULATION" for v in violations)


def test_current_reconciles_spurious_model_relationships_in_q3_frame(monkeypatch):
    malformed = {
        "domain": "aws_cloudtrail",
        "source_concepts": ["iam_user_access_key"],
        "scope_concepts": ["iam_resources"],
        "actor_concept": "iam_user",
        "event_concept": "access_attempt",
        "measure_concept": "distinct_error_types",
        "measure_role": "detailed_failure_description",
        "aggregation": "distinct_count",
        "ranking": "maximum",
        "temporal_constraint": "latest",
        "requested_output_concept": "iam_user_with_most_distinct_errors",
        "requires_reference_follow": True,
        "requires_external_enrichment": False,
        "primitives": ["EVENT_FILTER", "AGGREGATE", "RANK", "REFERENCE_FOLLOW"],
        "confidence": "high",
    }
    monkeypatch.setattr(frame_module, "structured_chat", lambda **kwargs: {"message": {"content": json.dumps(malformed)}})
    frame = build_investigation_frame(Q3, metrics={})
    assert frame["actor_concept"] == "IAM user access key"
    assert frame["event_concept"] is None
    assert frame["temporal_constraint"] == "none"
    assert frame["requires_reference_follow"] is False
    assert "REFERENCE_FOLLOW" not in frame["primitives"]
    assert "failed requests" in frame["scope_concepts"]


def test_exact_prior_second_query_is_double_blocked():
    st = _state()
    q = "index=botsv3 sourcetype=aws:cloudtrail eventSource=iam.amazonaws.com errorCode=* | stats values(errorCode) as errors by userIdentity.accessKeyId | eval distinct_errors=len(errors) | sort - distinct_errors | head 1"
    codes = {v["code"] for v in semantic_query_policy_violations(q, st, phase="CHECK")}
    assert "SEMANTIC_BINDING_QUERY_MISMATCH" in codes
    assert "MULTIVALUE_LEN_CARDINALITY_ERROR" in codes
    assert "INCOMPLETE_RANKING_POPULATION" in codes


def test_reconciled_q3_frame_enters_bootstrap_semantic_recovery():
    frame = heuristic_frame(Q3)
    bindings = resolve_semantic_bindings(frame, Q3)
    strategy = compose_query_strategy(
        Q3, ["aws_cloudtrail", "spl_distinct_count"],
        investigation_frame=frame, semantic_bindings=bindings,
    )
    assert strategy["bootstrap"] is True
    assert strategy["confidence"] == "high"
    assert strategy["semantic_review_skill"] == "result_semantics_recovery"
    assert "dc(errorMessage)" in strategy["primary_query"]
    assert "head 1" not in strategy["primary_query"].lower()
