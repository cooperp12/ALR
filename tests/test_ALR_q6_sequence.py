import json
from types import SimpleNamespace

import pytest

from commander_agent.core import q6_ubuntu_launch as q6


KEY = "AKIAABCDEFGHIJKLMNOP"
TEMP = "ASIAABCDEFGHIJKLMNOP"
USER = "example_user"



@pytest.fixture(autouse=True)
def compact_dynamic_scope(monkeypatch):
    monkeypatch.setattr(q6 if 'q6' in globals() else q, "Q6_DYNAMIC_WINDOW_MINUTES", (30,))
    monkeypatch.setattr(q6 if 'q6' in globals() else q, "Q6_SCOPE_STABILITY_REQUIRED", 0)

class FakeSession:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.queries = []

    async def call_tool(self, name, arguments=None):
        assert name == "search_oneshot"
        self.queries.append((arguments or {}).get("query", ""))
        payload = self.payloads.pop(0)
        return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(payload))])


def _payload(*rows):
    return {"events": list(rows), "event_count": len(rows)}


def _identity_row():
    return {
        "eventTime": "2018-08-20T09:16:12Z",
        "eventName": "GetSessionToken",
        "eventSource": "sts.amazonaws.com",
        "sourceIPAddress": "203.0.113.10",
        "access_key": KEY,
        "iam_user": USER,
        "iam_arn": "arn:aws:iam::111122223333:user/example_user",
        "account_id": "111122223333",
        "principal_id": "AIDAEXAMPLE",
        "derived_access_key": TEMP,
        "errorCode": "",
    }


def _launch_row(event_id="evt-1", image_id="ami-12345678", event_time="2018-08-20T09:16:14Z"):
    return {
        "eventTime": event_time,
        "eventID": event_id,
        "region": "eu-central-1",
        "access_key": TEMP,
        "iam_user": USER,
        "userAgent": "Boto3/1.7.62 Python/2.7.12 Linux/4.4.0",
        "image_id": image_id,
        "errorCode": "Client.UnauthorizedOperation",
        "eventName": "RunInstances",
        "eventSource": "ec2.amazonaws.com",
    }


def _raw_launch(row=None):
    row = row or _launch_row()
    raw = {
        "eventTime": row["eventTime"],
        "eventID": row["eventID"],
        "awsRegion": row["region"],
        "eventSource": "ec2.amazonaws.com",
        "eventName": "RunInstances",
        "userAgent": row.get("userAgent", ""),
        "errorCode": row.get("errorCode", ""),
        "userIdentity": {"accessKeyId": row["access_key"], "userName": row["iam_user"]},
        "requestParameters": {"instancesSet": {"items": [{"imageId": row["image_id"]}]}},
    }
    return {"_raw": json.dumps(raw)}


def test_benchmark_order_is_q1_q2_q3_q6():
    from pathlib import Path
    data = json.loads((Path(__file__).parents[1] / "benchmarks" / "botsv3_q1_q3_q6_benchmark.json").read_text())
    assert list(data) == ["Q1", "Q2", "Q3", "Q6"]


def test_sequence_state_uses_only_passed_prior_answers():
    results = [
        {"qid": "Q1", "passed": True, "metrics": {"final_validation_status": "validated"}, "answer": "111122223344"},
        {"qid": "Q2", "passed": True, "metrics": {"final_validation_status": "validated"}, "answer": "ExampleClient/1.0"},
        {"qid": "Q3", "passed": True, "metrics": {"final_validation_status": "validated"}, "answer": KEY},
        {"qid": "Q4", "passed": False, "answer": "should-not-enter-state"},
    ]
    state = q6.build_sequence_state(results)
    assert state["support_case_id"] == "111122223344"
    assert state["adversary_user_agent"] == "ExampleClient/1.0"
    assert state["compromised_access_key"] == KEY
    assert state["validated_qids"] == ["Q1", "Q2", "Q3"]


def test_identity_expansion_derives_user_and_sts_key_without_known_values():
    graph = q6._derive_identity_graph([_identity_row()], KEY)
    assert graph["status"] == "resolved"
    assert graph["iam_user"] == USER
    assert graph["derived_access_keys"] == [TEMP]
    assert graph["all_access_keys"] == [KEY, TEMP]


@pytest.mark.asyncio
async def test_q6_resolver_expands_identity_then_orders_first_and_enriches(monkeypatch, tmp_path):
    row = _launch_row()
    session = FakeSession([_payload(_identity_row()), _payload(row), _payload(_raw_launch(row))])

    monkeypatch.setattr(q6, "Q6_LEDGER", tmp_path / "q6.jsonl")
    monkeypatch.setattr(q6, "Q6_STAGE_TRACE", tmp_path / "q6-stage.json")
    monkeypatch.setattr(
        q6,
        "resolve_ubuntu_ami",
        lambda ami, region: {
            "release": {"version": "99.99", "series": "example"},
            "source": "test external evidence",
            "confidence": "high",
        },
    )
    monkeypatch.setattr(
        q6,
        "resolve_ubuntu_codename",
        lambda release: {"codename": "Example Eland", "source": "test official release evidence"},
    )

    metrics = {"search_calls": 0, "canonical_release_allows": 0}
    state = {
        "support_case_id": "111122223344",
        "adversary_user_agent": "ExampleClient/1.0",
        "compromised_access_key": KEY,
    }
    result = await q6.resolve_q6_from_sequence(session, state, metrics=metrics)

    assert result["status"] == "validated"
    assert result["answer"] == "Example Eland"
    assert result["resolved_iam_user"] == USER
    assert result["derived_access_keys"] == [TEMP]
    assert result["first_attempt"]["image_id"] == "ami-12345678"
    assert result["first_attempt"]["access_key"] == TEMP
    assert "GetSessionToken" not in session.queries[0]  # discover identity generically
    assert 'responseElements.credentials.accessKeyId' in session.queries[0]
    assert 'eventName="RunInstances"' in session.queries[1]
    assert f'iam_user="{USER}"' in session.queries[1]
    assert f'access_key="{TEMP}"' in session.queries[1]
    assert '"RunInstances"' in session.queries[2]
    assert metrics["q6_identity_expansions"] == 1
    assert metrics["q6_related_access_keys"] == 1
    assert metrics["q6_first_event_resolutions"] == 1
    assert metrics["q6_first_event_verifications"] == 1
    assert metrics["q6_enrichment_resolutions"] == 1
    assert metrics["final_validation_status"] == "validated"


def test_release_parser_extracts_descriptor_without_local_codename_catalogue():
    assert q6._release_from_text("ami-12345678 eu-west-1 ubuntu-example-99.99-server", require_ami="ami-12345678", region="eu-west-1") == {
        "version": "99.99", "series": "example"
    }
    assert q6._release_from_text("unrelated text", require_ami="ami-deadbeef") is None


def test_identity_expansion_builds_evidence_based_dynamic_window():
    graph = q6._derive_identity_graph([_identity_row()], KEY)
    window = q6._temporal_window(graph, 30)
    assert window["end_epoch"] - window["start_epoch"] == 3600
    query = q6._primary_query_for_identity(graph, window)
    assert "event_epoch>=" in query and "event_epoch<=" in query
    raw_query = q6._raw_launch_query(graph, window)
    assert "_time>=" in raw_query and "_time<=" in raw_query


@pytest.mark.asyncio
async def test_q6_can_pivot_on_stable_username_when_temp_key_is_not_projected(monkeypatch, tmp_path):
    identity = dict(_identity_row(), derived_access_key="")
    launch = _launch_row()
    session = FakeSession([_payload(identity), _payload(launch), _payload(_raw_launch(launch))])
    monkeypatch.setattr(q6, "Q6_LEDGER", tmp_path / "q6.jsonl")
    monkeypatch.setattr(q6, "Q6_STAGE_TRACE", tmp_path / "q6-stage.json")
    monkeypatch.setattr(q6, "resolve_ubuntu_ami", lambda ami, region: {
        "release": {"version": "99.99", "series": "example"},
        "source": "fixture",
    })
    monkeypatch.setattr(q6, "resolve_ubuntu_codename", lambda release: {
        "codename": "Example Eland", "source": "fixture release metadata"
    })
    result = await q6.resolve_q6_from_sequence(session, {"compromised_access_key": KEY})
    assert result["status"] == "validated"
    assert result["resolved_iam_user"] == USER
    assert result["derived_access_keys"] == []
    assert result["first_attempt"]["access_key"] == TEMP
