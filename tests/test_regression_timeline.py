from commander_agent.skills.bidirectional_timeframe.scripts import timeline_engine as te


def test_anchor_versioning_and_multiple_resource_relationships(tmp_path):
    te.TIMELINE_LEDGER_FILE = str(tmp_path / "timeline.jsonl")
    te.TIMELINE_ANCHOR_FILE = str(tmp_path / "anchors.json")
    te.RELATIONSHIP_LEDGER_FILE = str(tmp_path / "relationships.jsonl")

    case = {"case_id": "case-v", "anchor": None}
    evidence = {
        "evidence_id": "E1",
        "source": "splunk",
        "tool": "search_oneshot",
        "method_class": "targeted_rows",
        "query": "index=x",
    }
    events = [
        {
            "_time": "2018-08-20T10:00:00Z",
            "eventName": "CreateThing",
            "accessKeyId": "KEY-X",
            "userName": "alice",
            "sourceIPAddress": "1.2.3.4",
            "requestParameters": '{"userName":"target-a","roleName":"target-b"}',
        },
        {
            "_time": "2018-08-20T10:30:00Z",
            "eventName": "Later",
            "accessKeyId": "KEY-X",
        },
    ]
    te.append_timeline_events(case, evidence, events)

    a1 = te.set_anchor(case, "2018-08-20T10:00:00Z", evidence_id="E1", entity="KEY-X")
    a2 = te.set_anchor(case, "2018-08-20T10:30:00Z", evidence_id="E1", entity="KEY-X")
    assert a1["ok"] and a1["anchor"]["anchor_version"] == 1
    assert a2["ok"] and a2["anchor"]["anchor_version"] == 2

    rel = te.timeline_related("CreateThing", case_id="case-v")
    targets = {r["to"] for r in rel if r.get("relation") == "TARGETED"}
    assert "target-a" in targets
    assert "target-b" in targets
