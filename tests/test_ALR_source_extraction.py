import json
from pathlib import Path

from commander_agent.semantic.frame import heuristic_frame
from commander_agent.semantic.binding import resolve_semantic_bindings, binding_for
from commander_agent.semantic.contracts import build_source_contract, build_extraction_contract
from commander_agent.semantic.query_policy import semantic_query_policy_violations
from commander_agent.skills.strategy import compose_query_strategy
from commander_agent.state.extraction import (
    derive_extraction_candidate,
    build_dynamic_check_query,
    verify_extraction_candidate,
)
from commander_agent.state.release import canonical_release_status

Q1 = "Bud accidentally commits AWS access keys to an external code repository. Shortly after, he receives a notification from AWS that the account had been compromised. What is the support case ID that Amazon opens on his behalf?"
Q2 = "Using a leaked AWS key AKIAJOGCDXJ5NW5PXUPA, the adversary makes an unauthorized attempt to describe an account. What is the full user agent string of the application that originated the request?"
Q3 = "What IAM user access key generates the most distinct errors when attempting to access IAM resources?"


def _case(frame, contract, evidence=None):
    return {
        "case_id": "case-27",
        "question": Q2,
        "investigation_frame": frame,
        "semantic_bindings": resolve_semantic_bindings(frame, question=Q2),
        "source_contract": build_source_contract(frame, Q2),
        "extraction_contracts": [contract] if contract else [],
        "extraction_derivations": [],
        "extraction_verifications": [],
        "candidate_verifications": [],
        "canonical_release_history": [],
        "semantic_conflicts": [],
        "evidence_conflicts": [],
        "evidence": list(evidence or []),
        "discovery_state": {"observed_event_names": []},
    }


def test_q1_frame_routes_notification_support_case_to_email():
    frame = heuristic_frame(Q1)
    assert frame["domain"] == "email"
    assert frame["requested_output_concept"] == "support_case_id"
    assert "AWS notification email" in frame["source_concepts"]


def test_q2_frame_uses_client_identifier_role_not_error_message():
    frame = heuristic_frame(Q2)
    assert frame["domain"] == "aws_cloudtrail"
    assert frame["measure_role"] == "client_identifier"
    bindings = resolve_semantic_bindings(frame, question=Q2)
    output = binding_for(bindings, kind="output_field")
    assert output["field"] == "userAgent"
    assert output["role_required"] == "client_identifier"


def test_q1_contract_prefers_email_and_bootstraps_direct_extraction():
    frame = heuristic_frame(Q1)
    source = build_source_contract(frame, Q1)
    extraction = build_extraction_contract(frame, Q1, [], source)
    strategy = compose_query_strategy(Q1, ["email_investigation"], frame, [])
    assert source["source"] == "email"
    assert extraction["output_role"] == "support_case_identifier"
    assert strategy["resolver"] == "direct_extraction"
    assert "sourcetype=stream:smtp" in strategy["primary_query"]
    assert "support_case_id" in strategy["check_query"]


def test_q2_contract_requires_discovery_and_useragent():
    frame = heuristic_frame(Q2)
    source = build_source_contract(frame, Q2)
    bindings = resolve_semantic_bindings(frame, question=Q2)
    extraction = build_extraction_contract(frame, Q2, bindings, source)
    strategy = compose_query_strategy(Q2, ["aws_cloudtrail"], frame, bindings)
    assert extraction["output_field"] == "userAgent"
    assert extraction["entity_filters"]["userIdentity.accessKeyId"] == "AKIAJOGCDXJ5NW5PXUPA"
    assert extraction["discovery_before_specificity"] is True
    assert strategy["resolver"] == "direct_extraction"
    assert "eventName=" not in strategy["primary_query"]
    assert "userAgent" in strategy["primary_query"]


def test_q2_unobserved_specificity_is_blocked_then_allowed_after_discovery():
    frame = heuristic_frame(Q2)
    source = build_source_contract(frame, Q2)
    contract = build_extraction_contract(frame, Q2, resolve_semantic_bindings(frame, question=Q2), source)
    state = _case(frame, contract)
    bad = "index=botsv3 sourcetype=aws:cloudtrail userIdentity.accessKeyId=AKIAJOGCDXJ5NW5PXUPA eventName=DescribeAccount | fields userAgent"
    codes = {x["code"] for x in semantic_query_policy_violations(bad, state)}
    assert "UNOBSERVED_EVENT_SPECIFICITY" in codes
    broad = "index=botsv3 sourcetype=aws:cloudtrail userIdentity.accessKeyId=AKIAJOGCDXJ5NW5PXUPA | table eventName eventSource errorCode userAgent"
    assert not semantic_query_policy_violations(broad, state)
    state["discovery_state"]["observed_event_names"] = ["DescribeAccountAttributes"]
    observed = "index=botsv3 sourcetype=aws:cloudtrail userIdentity.accessKeyId=AKIAJOGCDXJ5NW5PXUPA eventName=DescribeAccountAttributes | fields userAgent"
    assert not semantic_query_policy_violations(observed, state)


def test_q1_cloudtrail_query_blocked_by_source_contract():
    frame = heuristic_frame(Q1)
    source = build_source_contract(frame, Q1)
    contract = build_extraction_contract(frame, Q1, [], source)
    state = _case(frame, contract)
    state["question"] = Q1
    state["source_contract"] = source
    bad = 'index=botsv3 sourcetype=aws:cloudtrail errorMessage=* | table errorCode'
    codes = {x["code"] for x in semantic_query_policy_violations(bad, state)}
    assert "SOURCE_CONTRACT_MISMATCH" in codes


def test_q2_derives_from_observed_semantic_event_then_builds_exact_check():
    frame = heuristic_frame(Q2)
    source = build_source_contract(frame, Q2)
    contract = build_extraction_contract(frame, Q2, resolve_semantic_bindings(frame, question=Q2), source)
    primary = {
        "evidence_id": "E1", "source": "splunk", "independence_key": "primary", "method_class": "targeted_rows",
        "machine_result": {"events": [
            {"userIdentity.accessKeyId": "AKIAJOGCDXJ5NW5PXUPA", "eventName": "CreateAccessKey", "errorCode": "AccessDenied", "userAgent": "OtherClient/1"},
            {"userIdentity.accessKeyId": "AKIAJOGCDXJ5NW5PXUPA", "eventName": "DescribeAccountAttributes", "eventSource": "ec2.amazonaws.com", "errorCode": "Client.UnauthorizedOperation", "userAgent": "ElasticWolf/5.1.6"},
        ]},
    }
    state = _case(frame, contract, [primary])
    derivation = derive_extraction_candidate(state)
    assert derivation["candidate"] == "ElasticWolf/5.1.6"
    assert derivation["selected_event"]["eventName"] == "DescribeAccountAttributes"
    check = build_dynamic_check_query(state, derivation)
    assert "eventName=DescribeAccountAttributes" in check
    assert "values(userAgent)" in check
    assert "DescribeAccountAttributes" in state["discovery_state"]["observed_event_names"]


def test_direct_extraction_release_requires_two_support_paths():
    frame = heuristic_frame(Q2)
    source = build_source_contract(frame, Q2)
    contract = build_extraction_contract(frame, Q2, resolve_semantic_bindings(frame, question=Q2), source)
    primary = {
        "evidence_id": "E1", "source": "splunk", "independence_key": "primary", "method_class": "targeted_rows",
        "machine_result": {"events": [{"userIdentity.accessKeyId": "AKIAJOGCDXJ5NW5PXUPA", "eventName": "DescribeAccountAttributes", "errorCode": "Denied", "userAgent": "ElasticWolf/5.1.6"}]},
    }
    check = {
        "evidence_id": "E2", "source": "splunk", "independence_key": "check", "method_class": "aggregation_values",
        "machine_result": {"events": [{"eventName": "DescribeAccountAttributes", "observed_user_agents": "ElasticWolf/5.1.6", "matching_events": "1"}]},
    }
    state = _case(frame, contract, [primary, check])
    d = derive_extraction_candidate(state)
    # Derivation scans newest first; ensure it is anchored to the primary event if the check lacks the output field.
    assert d["candidate"] == "ElasticWolf/5.1.6"
    v = verify_extraction_candidate(state, d, check_evidence_id="E2")
    assert v and len(v["support_ids"]) == 2
    release = canonical_release_status(state, candidate="ElasticWolf/5.1.6")
    assert release["ready"] is True
    assert release["contract_kind"] == "extraction"


def test_q3_still_uses_measurement_semantics_recovery():
    frame = heuristic_frame(Q3)
    bindings = resolve_semantic_bindings(frame, question=Q3)
    strategy = compose_query_strategy(Q3, ["aws_cloudtrail", "spl_distinct_count"], frame, bindings)
    assert strategy["resolver"] == "result_semantics_recovery"
    assert "dc(errorMessage)" in strategy["primary_query"]


def test_benchmark_package_has_exactly_three_cases():
    data = json.loads(Path("benchmarks/botsv3_q1_q3_benchmark.json").read_text(encoding="utf-8"))
    assert list(data) == ["Q1", "Q2", "Q3"]
    assert all(data[qid].get("question") for qid in data)

def test_q1_router_does_not_load_cloudtrail_contract_against_email_source():
    from commander_agent.skills.registry import heuristic_skills
    skills = heuristic_skills(Q1)
    assert "email_investigation" in skills
    assert "aws_cloudtrail" not in skills
    assert "external_enrichment" not in skills

def test_model_frame_cannot_rebind_q1_support_case_to_cloudtrail(monkeypatch):
    import commander_agent.semantic.frame as fm
    wrong = {
        "domain": "aws_cloudtrail",
        "source_concepts": ["aws_access_keys"],
        "scope_concepts": ["account_compromise_notification"],
        "actor_concept": "bud",
        "event_concept": "accidental_commit_and_detection",
        "measure_concept": "support_case_id",
        "measure_role": "failure_category",
        "aggregation": "none",
        "ranking": "none",
        "temporal_constraint": "none",
        "requested_output_concept": "support_case_id",
        "requires_reference_follow": False,
        "requires_external_enrichment": False,
        "primitives": ["SOURCE_SELECT", "DIRECT_EXTRACT"],
        "confidence": "high",
    }
    monkeypatch.setattr(fm, "structured_chat", lambda **kwargs: {"message": {"content": json.dumps(wrong)}})
    reconciled = fm.build_investigation_frame(Q1, metrics={})
    assert reconciled["domain"] == "email"
    assert reconciled["requested_output_concept"] == "support_case_id"
    bindings = resolve_semantic_bindings(reconciled, question=Q1)
    assert all(b.get("field") != "errorCode" for b in bindings)


def test_model_frame_cannot_rebind_q2_useragent_to_error_message(monkeypatch):
    import commander_agent.semantic.frame as fm
    wrong = {
        "domain": "aws_cloudtrail",
        "source_concepts": ["leaked_aws_access_key", "unauthorized_api_call"],
        "scope_concepts": ["account_description_attempt"],
        "actor_concept": "adversary",
        "event_concept": "api_request",
        "measure_concept": "user_agent_string",
        "measure_role": "detailed_failure_description",
        "aggregation": "none",
        "ranking": "none",
        "temporal_constraint": "none",
        "requested_output_concept": "full_user_agent_string",
        "requires_reference_follow": False,
        "requires_external_enrichment": False,
        "primitives": ["SOURCE_SELECT", "FIELD_ROLE_RESOLVE", "DIRECT_EXTRACT"],
        "confidence": "high",
    }
    monkeypatch.setattr(fm, "structured_chat", lambda **kwargs: {"message": {"content": json.dumps(wrong)}})
    reconciled = fm.build_investigation_frame(Q2, metrics={})
    assert reconciled["measure_role"] == "client_identifier"
    assert reconciled["requested_output_concept"] == "client identifier"
    output = binding_for(resolve_semantic_bindings(reconciled, question=Q2), kind="output_field")
    assert output["field"] == "userAgent"

def test_direct_canonical_release_forces_supervisor_to_validation():
    from commander_agent.skills.investigation_supervisor.scripts.reviewer import _deterministic_guard
    frame = heuristic_frame(Q2)
    source = build_source_contract(frame, Q2)
    contract = build_extraction_contract(frame, Q2, resolve_semantic_bindings(frame, question=Q2), source)
    primary = {
        "evidence_id": "E1", "source": "splunk", "independence_key": "primary", "method_class": "targeted_rows",
        "machine_result": {"events": [{"userIdentity.accessKeyId": "AKIA" + "JOGCDXJ5NW5PXUPA", "eventName": "DescribeAccountAttributes", "errorCode": "Denied", "userAgent": "ElasticWolf/5.1.6"}]},
    }
    check = {
        "evidence_id": "E2", "source": "splunk", "independence_key": "check", "method_class": "aggregation_values",
        "machine_result": {"events": [{"eventName": "DescribeAccountAttributes", "observed_user_agents": "ElasticWolf/5.1.6"}]},
    }
    state = _case(frame, contract, [primary, check])
    d = derive_extraction_candidate(state)
    assert verify_extraction_candidate(state, d, check_evidence_id="E2")
    state["orchestration"] = {"stage":"RECOVERY","current_goal":"x","remaining_gap":"x","goal_revision":1,"goal_signature":"x","attempts":[],"exhausted":[],"irrelevant":[],"blocked":[],"recommended":[],"supervisor_history":[],"irrelevant_by_version":{},"irrelevant_by_goal":{}}
    proposed = {
        "evaluation":"PARTIAL","goal_satisfied":False,"remaining_gap":"keep searching",
        "next_capabilities":[],"irrelevant_capabilities":[],"invalidate_from_stage":None,
        "allow_validation":False,"allow_stop":False,"reason":"model wants more work",
    }
    guarded = _deterministic_guard(proposed, state, candidate="ElasticWolf/5.1.6")
    assert guarded["allow_validation"] is True
    assert guarded["canonical_release_ready"] is True
    assert guarded["goal_satisfied"] is True


def test_q1_live_shape_multivalue_check_derives_and_verifies_from_raw_message():
    from commander_agent.state.evidence import current_progress_version
    frame = heuristic_frame(Q1)
    source = build_source_contract(frame, Q1)
    contract = build_extraction_contract(frame, Q1, [], source)
    primary = {
        "evidence_id": "E1", "source": "splunk", "independence_key": "primary-q1", "method_class": "targeted_rows",
        "query": 'index=botsv3 earliest=0 sourcetype=stream:smtp "support case" | table _raw',
        "machine_result": {"events": [{"subject": "AWS Security Notification", "_raw": "Amazon Web Services has opened support case 5244329601 regarding compromised access keys."}]},
    }
    check = {
        "evidence_id": "E2", "source": "splunk", "independence_key": "check-q1", "method_class": "aggregation_values",
        "query": 'index=botsv3 earliest=0 sourcetype=stream:smtp case | rex ... | stats values(support_case_id) AS support_case_ids',
        "machine_result": {"events": [{"support_case_ids": ["5244329601"], "matching_messages": "1"}]},
    }
    state = _case(frame, contract, [primary, check])
    state["question"] = Q1
    state["source_contract"] = source
    before = current_progress_version(state)
    d = derive_extraction_candidate(state)
    assert d["candidate"] == "5244329601"
    after_derive = current_progress_version(state)
    assert after_derive[1] > before[1]
    v = verify_extraction_candidate(state, d)
    assert v and len(v["support_ids"]) == 2
    after_verify = current_progress_version(state)
    assert after_verify[1] > after_derive[1]
    assert canonical_release_status(state, candidate="5244329601")["ready"] is True


def test_q1_strategy_preserves_raw_message_and_uses_multivalue_check():
    frame = heuristic_frame(Q1)
    strategy = compose_query_strategy(Q1, ["email_investigation"], frame, [])
    assert "_raw" in strategy["primary_query"]
    assert "message_text=coalesce" in strategy["primary_query"]
    assert "support_case_ids" in strategy["check_query"]
    assert strategy["dynamic_check"] is True


def test_q2_primary_projection_preserves_entity_filter_field():
    frame = heuristic_frame(Q2)
    bindings = resolve_semantic_bindings(frame, question=Q2)
    strategy = compose_query_strategy(Q2, ["aws_cloudtrail"], frame, bindings)
    assert "userIdentity.accessKeyId eventSource eventName" in strategy["primary_query"]


def test_q2_query_scoped_identity_can_support_projection_that_omits_identity_field():
    frame = heuristic_frame(Q2)
    source = build_source_contract(frame, Q2)
    contract = build_extraction_contract(frame, Q2, resolve_semantic_bindings(frame, question=Q2), source)
    primary = {
        "evidence_id": "E1", "source": "splunk", "independence_key": "primary-q2", "method_class": "targeted_rows",
        "query": "index=botsv3 earliest=0 sourcetype=aws:cloudtrail userIdentity.accessKeyId=AKIAJOGCDXJ5NW5PXUPA | table eventName eventSource errorCode userAgent",
        "machine_result": {"events": [
            {"eventName": "DescribeAccountAttributes", "eventSource": "ec2.amazonaws.com", "errorCode": "Client.UnauthorizedOperation", "userAgent": "ElasticWolf/5.1.6"}
        ]},
    }
    state = _case(frame, contract, [primary])
    d = derive_extraction_candidate(state)
    assert d and d["candidate"] == "ElasticWolf/5.1.6"
    assert d["selected_event"]["eventName"] == "DescribeAccountAttributes"
