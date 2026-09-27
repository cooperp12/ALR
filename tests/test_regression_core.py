import json
from pathlib import Path

from commander_agent.skills.spl_distinct_count.scripts.query_templates import cloudtrail_distinct_plan
from commander_agent.semantic.frame import heuristic_frame
from commander_agent.semantic.binding import resolve_semantic_bindings
from commander_agent.skills.structured_output_validation.scripts.schema import (
    validate_search_arguments,
    validate_search_result_text,
    strict_json_roundtrip,
)
from commander_agent.skills.bidirectional_timeframe.scripts import timeline_engine as te


def test_distinct_plan_filters_success_and_is_dynamic():
    q = "What IAM user access key generates the most distinct errors when attempting to access IAM resources?"
    frame = heuristic_frame(q)
    bindings = resolve_semantic_bindings(frame, question=q)
    plan = cloudtrail_distinct_plan(q, frame, bindings)
    assert "eventSource=iam.amazonaws.com" in plan["primary_query"]
    assert "errorCode=*" in plan["primary_query"]  # failure population
    assert "dc(errorMessage)" in plan["primary_query"]  # requested measurement
    assert "values(errorMessage)" in plan["check_query"]
    assert "population_keys" in plan["primary_query"]
    assert "where distinct_count=max_distinct" not in plan["primary_query"]
    # No answer/key literals should be required to build the query.
    assert "AKIA" not in plan["primary_query"]



def test_distinct_plan_delegates_semantics_to_recovery_skill():
    plan = cloudtrail_distinct_plan(
        "Which account has the most distinct failed actions?"
    )
    assert plan["semantic_review_skill"] == "result_semantics_recovery"
    module_text = Path(
        "commander_agent/skills/spl_distinct_count/scripts/query_templates.py"
    ).read_text(encoding="utf-8")
    assert "resolve_distinct_winner" not in module_text


def test_structured_query_and_result_validation():
    ok, args, errors = validate_search_arguments({
        "query": "index=botsv3 | stats count",
        "max_count": 20,
        "output_format": "json",
    })
    assert ok and not errors
    assert args["max_count"] == 20

    assert validate_search_result_text(json.dumps({"event_count": 1, "events": [{"a": 1}]}))["ok"]
    failed = validate_search_result_text(json.dumps({"error": "Search failed"}))
    assert failed["ok"] is False
    assert failed["kind"] == "tool_error"

    ok, _, _ = strict_json_roundtrip({"a": {1, 2}})
    assert ok is False


def test_timeline_anchor_and_relative_position(tmp_path):
    te.TIMELINE_LEDGER_FILE = str(tmp_path / "timeline.jsonl")
    te.TIMELINE_ANCHOR_FILE = str(tmp_path / "anchors.json")
    te.RELATIONSHIP_LEDGER_FILE = str(tmp_path / "relationships.jsonl")

    case = {"case_id": "case-1", "anchor": None}
    evidence = {
        "evidence_id": "E1",
        "source": "splunk",
        "tool": "search_oneshot",
        "method_class": "targeted_rows",
        "query": "index=x",
    }
    events = [
        {"_time": "2018-08-20T10:00:00Z", "eventName": "Before", "accessKeyId": "KEY-X"},
        {"_time": "2018-08-20T10:30:00Z", "eventName": "Anchor", "accessKeyId": "KEY-X", "userName": "alice"},
        {"_time": "2018-08-20T10:45:00Z", "eventName": "After", "accessKeyId": "KEY-X"},
    ]
    count, rows = te.append_timeline_events(case, evidence, events)
    assert count == 3

    result = te.set_anchor(
        case,
        "2018-08-20T10:30:00Z",
        evidence_id="E1",
        reason="test",
        entity="KEY-X",
    )
    assert result["ok"] is True
    assert result["anchor"]["anchor_version"] == 1

    before = te.timeline_before("case-1", minutes=60, entity="KEY-X")
    after = te.timeline_after("case-1", minutes=60, entity="KEY-X")
    assert before["ok"] and before["events"][0]["direction_relative_anchor"] == "BEFORE"
    assert after["ok"] and after["events"][0]["direction_relative_anchor"] == "AFTER"
    assert before["events"][0]["seconds_from_anchor"] == -1800.0
    assert after["events"][0]["seconds_from_anchor"] == 900.0

    related = te.timeline_related("KEY-X", case_id="case-1")
    assert related
    assert any(r["relation"] in {"USED_BY", "PERFORMED"} for r in related)


def test_nested_unauthorized_is_classified_non_retryable():
    payload = json.dumps({
        "error": "Search failed",
        "details": {
            "error": json.dumps({
                "messages": [{"type": "ERROR", "text": "Unauthorized"}]
            })
        },
    })
    result = validate_search_result_text(payload)
    assert result["ok"] is False
    assert result["kind"] == "authorization_failure"
    assert result["failure"]["fatal"] is True
    assert result["failure"]["retryable"] is False
