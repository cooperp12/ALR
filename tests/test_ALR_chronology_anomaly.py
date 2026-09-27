import json
from types import SimpleNamespace
from pathlib import Path

import pytest

from commander_agent.core import q6_ubuntu_launch as q6
from commander_agent.core import q6_anomaly_review as ar
from commander_agent.skills.bidirectional_timeframe.scripts import timeline_engine as te


def _configure_timeline(monkeypatch, tmp_path):
    monkeypatch.setattr(te, "TIMELINE_LEDGER_FILE", str(tmp_path / "timeline.jsonl"))
    monkeypatch.setattr(te, "TIMELINE_ANCHOR_FILE", str(tmp_path / "anchors.json"))
    monkeypatch.setattr(te, "RELATIONSHIP_LEDGER_FILE", str(tmp_path / "relationships.jsonl"))


def _write_timeline_events_fast(case, evidence, events):
    """Seed large chronology fixtures in one write; runtime append semantics are tested elsewhere."""
    rows = [te.timeline_row_from_event(case, evidence, event) for event in events]
    rows = [row for row in rows if row]
    Path(te.TIMELINE_LEDGER_FILE).write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    Path(te.RELATIONSHIP_LEDGER_FILE).write_text("", encoding="utf-8")
    return rows


def test_timeline_map_finds_true_first_before_applying_limit(monkeypatch, tmp_path):
    _configure_timeline(monkeypatch, tmp_path)
    case = {"case_id": "many", "anchor": None}
    evidence = {"evidence_id": "E", "source": "splunk", "tool": "search_oneshot", "method_class": "chronological_first", "query": "index=x"}
    events = []
    # 121 events is sufficient to reproduce the old newest-100-then-sort defect
    # while keeping setup regression tests fast on Windows filesystems.
    for i in range(121):
        minute, second = divmod(i + 1, 60)
        events.append({
            "_time": f"2018-01-01T00:{minute:02d}:{second:02d}Z",
            "eventName": "RunInstances",
            "accessKeyId": "ASIAABCDEFGHIJKLMNOP",
            "userName": "alice",
            "awsRegion": "us-test-1",
            "requestParameters": {"instancesSet": {"items": [{"imageId": "ami-12345678"}]}},
        })
    _write_timeline_events_fast(case, evidence, events)
    mapped = te.timeline_map(case_id="many", limit=100)
    assert len(mapped["events"]) == 100
    assert mapped["events"][0]["event_time"] == "2018-01-01T00:00:01Z"


def test_timeline_after_keeps_events_closest_to_anchor_with_large_population(monkeypatch, tmp_path):
    _configure_timeline(monkeypatch, tmp_path)
    case = {"case_id": "after", "anchor": None}
    evidence = {"evidence_id": "E", "source": "splunk", "tool": "search_oneshot", "method_class": "chronological_first", "query": "index=x"}
    events = [{"_time": "2018-01-01T00:00:00Z", "eventName": "Anchor", "accessKeyId": "KEY"}]
    for i in range(1, 301):
        minute, second = divmod(i, 60)
        events.append({"_time": f"2018-01-01T00:{minute:02d}:{second:02d}Z", "eventName": "RunInstances", "accessKeyId": "KEY"})
    _write_timeline_events_fast(case, evidence, events)
    te.set_anchor(case, "2018-01-01T00:00:00Z", evidence_id="E", reason="test", entity="KEY")
    after = te.timeline_after("after", minutes=10, entity="KEY", limit=5)
    assert [x["seconds_from_anchor"] for x in after["events"]] == [1.0, 2.0, 3.0, 4.0, 5.0]


def test_q6_timeline_first_uses_complete_population(monkeypatch, tmp_path):
    _configure_timeline(monkeypatch, tmp_path)
    case = {"case_id": "q6-many", "anchor": None}
    evidence = {"evidence_id": "Q6", "source": "splunk", "tool": "search_oneshot", "method_class": "chronological_first", "query": "index=x"}
    launches = []
    events = []
    for i in range(160):
        minute, second = divmod(i + 1, 60)
        t = f"2018-01-01T00:{minute:02d}:{second:02d}Z"
        event_id = f"e{i:03d}"
        launches.append({"event_time": t, "epoch": 1514764801 + i, "event_id": event_id,
                         "region": "us-test-1", "image_id": "ami-12345678", "iam_user": "alice", "access_key": "ASIAABCDEFGHIJKLMNOP"})
        events.append({"_time": t, "eventName": "RunInstances", "eventID": event_id,
                       "accessKeyId": "ASIAABCDEFGHIJKLMNOP", "userName": "alice", "awsRegion": "us-test-1",
                       "requestParameters": {"instancesSet": {"items": [{"imageId": "ami-12345678"}]}}})
    _write_timeline_events_fast(case, evidence, events)
    selected, mapped = q6._timeline_first_launch("q6-many", launches, {})
    assert [x["event_id"] for x in selected] == ["e000"]
    assert mapped["chronology_population_count"] == 160
    assert mapped["first_event_time"] == "2018-01-01T00:00:01Z"


def test_raw_boundary_verification_does_not_reuse_spath_route():
    identity = {"iam_user": "alice", "all_access_keys": ["AKIAABCDEFGHIJKLMNOP", "ASIAABCDEFGHIJKLMNOP"]}
    q = q6._raw_boundary_verification_query(identity, {"start_epoch": 1, "end_epoch": 2})
    assert "spath" not in q.lower()
    assert "eventstats min(_time) AS first_raw_time" in q
    assert "where _time=first_raw_time" in q


@pytest.mark.asyncio
async def test_route_disagreement_switches_to_raw_and_escalates_once(monkeypatch):
    key = "AKIAABCDEFGHIJKLMNOP"
    temp = "ASIAABCDEFGHIJKLMNOP"
    identity = {"iam_user": "alice", "all_access_keys": [key, temp],
                "scope_basis_start_epoch": 1514764800.0, "scope_basis_end_epoch": 1514764800.0,
                "scope_basis_event_count": 1}
    raw_event = {"eventTime": "2018-01-01T00:00:10Z", "eventName": "RunInstances", "eventSource": "ec2.amazonaws.com",
                 "eventID": "first", "awsRegion": "us-test-1",
                 "userIdentity": {"accessKeyId": temp, "userName": "alice"},
                 "requestParameters": {"instancesSet": {"items": [{"imageId": "ami-12345678"}]}}}

    class Session:
        def __init__(self):
            self.payloads = [
                {"events": []},
                {"events": [{"_raw": json.dumps(raw_event)}]},
                {"events": [{"_raw": json.dumps(raw_event)}]},
            ]
        async def call_tool(self, name, arguments):
            return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(self.payloads.pop(0)))])

    seen = []
    monkeypatch.setattr(q6, "Q6_DYNAMIC_WINDOW_MINUTES", (5, 15))
    monkeypatch.setattr(q6, "Q6_SCOPE_STABILITY_REQUIRED", 1)
    monkeypatch.setattr(q6, "_review_anomaly", lambda record, metrics, kind, packet: seen.append((kind, packet)) or {"kind": kind})
    launches, rows, window, mode, history, error = await q6._dynamic_launch_search(Session(), identity, record={}, metrics={})
    assert error is None
    assert mode == "raw_identity_recovery"
    assert launches[0]["event_id"] == "first"
    assert seen and seen[0][0] == "source_route_disagreement"
    assert len(history) == 3


def test_worker_and_supervisor_anomaly_roles_are_bounded(monkeypatch):
    calls = []
    worker = {"diagnosis": "parsed route contradicted by raw evidence", "recommended_action": "switch_to_raw_recovery",
              "confidence": "high", "reason": "raw evidence exists", "checks": ["preserve identity"]}
    supervisor = {"decision": "approve", "approved_action": "switch_to_raw_recovery", "confidence": "high",
                  "reason": "bounded failover preserves invariants", "invariants": ["FIRST remains deterministic"]}

    def fake_structured_chat(*, role, **kwargs):
        calls.append(role)
        payload = worker if role == "investigator" else supervisor
        return {"message": {"content": json.dumps(payload)}}

    monkeypatch.setattr(ar, "structured_chat", fake_structured_chat)
    out = ar.review_q6_anomaly("source_route_disagreement", {"parsed_candidate_count": 0, "raw_candidate_count": 10}, {})
    assert calls == ["investigator", "supervisor"]
    assert out["deterministic_action"] == "switch_to_raw_recovery"
    assert out["worker_agrees_with_default"] is True
    assert out["supervisor_agrees_with_default"] is True


def test_anomaly_prompts_contain_no_benchmark_oracle_or_known_answer_literals():
    source = Path("commander_agent/core/q6_anomaly_review.py").read_text(encoding="utf-8")
    assert "score_oracle.sha256" not in source
    assert "from commander_agent.evaluation.scorer" not in source
    assert "benchmarks/" not in source
    # The anomaly layer must not carry any local Ubuntu answer catalogue.
    assert "UBUNTU_RELEASES" not in source


def test_iteration_specific_source_reads_are_windows_utf8_safe():
    source = Path("tests/test_ALR_second_order.py").read_text(encoding="utf-8")
    assert ".read_text()" not in source
