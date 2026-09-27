import json
from pathlib import Path

from commander_agent.skills.contracts import infer_investigation_type
from commander_agent.skills.investigation_supervisor.scripts import reviewer as supervisor_reviewer
from commander_agent.state.orchestration import (
    RECOVERY_CAPABILITIES,
    SAME_SCOPE_CAPABILITIES,
    available_recovery_capabilities,
    can_stop_unresolved,
    ensure_orchestration_state,
    record_capability_attempt,
)


def _state(version=1):
    return {
        "case_id": "case-supervisor",
        "evidence": [],
        "signature_to_evidence": {},
        "evidence_version": version,
        "semantic_reviews": [],
        "semantic_findings": [],
        "candidate_quarantine": [],
        "semantic_action_signatures": set(),
        "semantic_reviews_by_version": {},
        "active_skills": [],
        "scope_contract": {"base_scope_spl": "index=x source=y"},
    }


def test_primary_classification_is_not_overridden_by_optional_temporal_skill():
    question = "Which account has the most distinct failures?"
    assert infer_investigation_type(question, ["spl_distinct_count", "temporal_pattern_analysis"]) == "aggregation"


def test_hierarchy_is_data_driven_from_skill_graph_file():
    data = json.loads(Path("commander_agent/skills/hierarchy.json").read_text(encoding="utf-8"))
    names = [x["name"] for x in data["recovery_capabilities"]]
    assert names == RECOVERY_CAPABILITIES
    assert "temporal_pattern_analysis" in names
    assert "scope_expansion" in names
    assert len(SAME_SCOPE_CAPABILITIES) >= 1


def test_scope_expansion_is_not_eligible_while_same_scope_routes_remain():
    state = _state()
    ensure_orchestration_state(state)
    available = available_recovery_capabilities(state)
    assert "scope_expansion" not in available
    assert any(x in available for x in SAME_SCOPE_CAPABILITIES)
    assert can_stop_unresolved(state) is False


def test_stalled_capability_is_not_retried_until_supervisor_changes_the_goal():
    state = _state(version=3)
    ensure_orchestration_state(state)
    assert "semantic_field_review" in available_recovery_capabilities(state)
    record_capability_attempt(
        state,
        "semantic_field_review",
        "no_new_evidence",
        evidence_before=3,
        evidence_after=3,
    )
    assert "semantic_field_review" not in available_recovery_capabilities(state)
    state["evidence_version"] = 4
    # New evidence alone does not reopen a route when the unresolved goal is unchanged.
    assert "semantic_field_review" not in available_recovery_capabilities(state)
    from commander_agent.state.orchestration import record_supervisor_evaluation
    record_supervisor_evaluation(state, {
        "evaluation": "PARTIAL",
        "goal_satisfied": False,
        "remaining_gap": "A materially different semantic gap is now unresolved.",
        "next_capabilities": ["semantic_field_review"],
        "irrelevant_capabilities": [],
        "invalidate_from_stage": "PRIMARY_ANALYSIS",
        "allow_validation": False,
        "allow_stop": False,
        "reason": "New evidence changed the question that this capability must answer.",
    })
    assert "semantic_field_review" in available_recovery_capabilities(state)


def test_supervisor_mechanical_guard_denies_premature_stop():
    state = _state(version=2)
    ensure_orchestration_state(state)
    proposed = {
        "evaluation": "BLOCKED",
        "goal_satisfied": False,
        "remaining_gap": "Ambiguous result.",
        "next_capabilities": [],
        "irrelevant_capabilities": [],
        "invalidate_from_stage": None,
        "allow_validation": False,
        "allow_stop": True,
        "reason": "One route failed.",
    }
    guarded = supervisor_reviewer._deterministic_guard(proposed, state, candidate="")
    assert guarded["allow_stop"] is False
    assert guarded["next_capabilities"]
    assert "same-scope routes remain" in guarded["reason"]


def test_investigator_action_contract_rejects_noneligible_capability():
    from commander_agent.skills.result_semantics_recovery.scripts.reviewer import validate_action_contract
    obj = {
        "decision": "RUN_TEMPORAL_ANALYSIS",
        "capability": "temporal_pattern_analysis",
        "candidate_status": "ambiguous",
        "reason": "Try temporal evidence.",
        "candidate": None,
        "recommended_spl": None,
        "support_ids": [],
        "analysis_request": {
            "group_field": "actor",
            "value_field": "status",
            "time_field": "_time",
            "measures": ["event_count"],
            "comparison_feature": None,
        },
        "scope_change": {"requested": False, "reason": None},
    }
    errors = validate_action_contract(obj, current_candidate="", eligible_capabilities=["schema_inspection"])
    assert any("not currently eligible" in x for x in errors)


def test_setup_uses_project_local_pytest_temp_directory():
    setup = Path("setup-ALR.cmd").read_text(encoding="utf-8")
    assert "--basetemp=.pytest_tmp" in setup
    assert 'rmdir /s /q ".pytest_tmp"' in setup


def test_defer_capability_contract_allows_clean_handoff_without_spl():
    from commander_agent.skills.result_semantics_recovery.scripts.reviewer import validate_action_contract
    obj = {
        "decision": "DEFER_CAPABILITY",
        "capability": "semantic_field_review",
        "candidate_status": "ambiguous",
        "reason": "The current evidence is insufficient to establish field semantics; schema evidence is needed.",
        "candidate": None,
        "recommended_spl": None,
        "support_ids": ["E1"],
        "suggested_capabilities": ["schema_inspection"],
        "scope_change": {"requested": False, "reason": None},
    }
    assert validate_action_contract(
        obj, current_candidate="", eligible_capabilities=["semantic_field_review"], assigned_capability="semantic_field_review"
    ) == []


def test_assigned_capability_cannot_silently_morph_into_sibling():
    from commander_agent.skills.result_semantics_recovery.scripts.reviewer import validate_action_contract
    obj = {
        "decision": "INVESTIGATE_SCHEMA",
        "capability": "schema_inspection",
        "candidate_status": "ambiguous",
        "reason": "Inspect schema.",
        "candidate": None,
        "recommended_spl": "index=x | table actor code detail | head 20",
        "support_ids": ["E1"],
        "scope_change": {"requested": False, "reason": None},
    }
    errors = validate_action_contract(
        obj, current_candidate="", eligible_capabilities=["semantic_field_review"], assigned_capability="semantic_field_review"
    )
    assert any("assigned capability" in e or "not currently eligible" in e for e in errors)
