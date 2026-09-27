import json
from pathlib import Path

from commander_agent.semantic.frame import heuristic_frame
from commander_agent.semantic.binding import resolve_semantic_bindings, binding_for
from commander_agent.semantic.conflicts import detect_and_rebind
from commander_agent.skills.spl_distinct_count.scripts.query_templates import cloudtrail_distinct_plan
from commander_agent.state.evidence_store import ingest_machine_evidence, derive_candidate_from_contract
from commander_agent.state.semantic_state import record_semantic_bindings
import commander_agent.core.measurement_resolution as mr


Q3 = "What IAM user access key generates the most distinct errors when attempting to access IAM resources?"


def state(case_id="c"):
    return {
        "case_id": case_id,
        "question": Q3,
        "evidence": [],
        "evidence_version": 1,
        "observations": [],
        "observation_fingerprints": set(),
        "measurement_candidates": [],
        "measurement_candidate_fingerprints": set(),
        "measurement_contracts": [],
        "active_measurement_contract": None,
        "candidate_derivations": [],
        "evidence_conflicts": [],
        "investigation_frame": None,
        "semantic_bindings": [],
        "semantic_conflicts": [],
        "semantic_relationships": [],
    }


def test_q3_frame_separates_failure_population_from_measurement():
    frame = heuristic_frame(Q3)
    assert frame["domain"] == "aws_cloudtrail"
    assert frame["measure_concept"] == "error"
    assert frame["measure_role"] == "detailed_failure_description"
    assert frame["aggregation"] == "distinct_count"
    assert frame["ranking"] == "maximum"
    assert "failed requests" in frame["scope_concepts"]

    bindings = resolve_semantic_bindings(frame, Q3)
    failure = binding_for(bindings, kind="presence_filter")
    measure = binding_for(bindings, kind="measurement_field")
    group = binding_for(bindings, kind="group_field")
    assert failure["field"] == "errorCode"
    assert measure["field"] == "errorMessage"
    assert group["field"] == "userIdentity.accessKeyId"


def test_q3_query_is_bound_before_results_and_python_ranks_full_population():
    frame = heuristic_frame(Q3)
    bindings = resolve_semantic_bindings(frame, Q3)
    plan = cloudtrail_distinct_plan(Q3, frame, bindings)
    q = plan["primary_query"]
    assert "dc(errorMessage) AS distinct_count" in q
    assert "errorCode=*" in q
    assert "eventSource=iam.amazonaws.com" in q
    assert "eventstats count AS population_keys" in q
    assert "where distinct_count=max_distinct" not in q


def test_semantic_binding_promotes_matching_measurement_without_model(monkeypatch):
    st = state("q3")
    frame = heuristic_frame(Q3)
    st["investigation_frame"] = frame
    record_semantic_bindings(st, resolve_semantic_bindings(frame, Q3))
    query = cloudtrail_distinct_plan(Q3, frame, st["semantic_bindings"])["primary_query"]
    evidence = {"evidence_id":"E1","query":query,"result_limit":50}
    payload = {"events":[
        {"userIdentity.accessKeyId":"KEY-A","distinct_count":"1","matching_events":"9","population_keys":"3"},
        {"userIdentity.accessKeyId":"KEY-B","distinct_count":"5","matching_events":"6","population_keys":"3"},
        {"userIdentity.accessKeyId":"KEY-C","distinct_count":"1","matching_events":"2","population_keys":"3"},
    ],"rows_omitted":0}
    ingest_machine_evidence(st,evidence,payload)
    monkeypatch.setattr(mr, "structured_chat", lambda *a, **k: (_ for _ in ()).throw(AssertionError("model must not be called")))
    metrics={}
    contract=mr.resolve_measurement_semantics(question=Q3,case_state=st,metrics=metrics)
    assert contract["source_expression"] == "errorMessage"
    assert contract["evidence_id"] == "E1"
    assert metrics["semantic_binding_contract_promotions"] == 1
    derived=derive_candidate_from_contract(st,contract)
    assert derived["status"] == "UNIQUE_MAXIMUM"
    assert derived["winners"] == ["KEY-B"]
    assert derived["maximum_value"] == 5


def test_coarse_category_binding_is_invalidated_by_one_to_many_detail_evidence():
    st=state("rebind")
    frame=heuristic_frame(Q3)
    st["investigation_frame"]=frame
    bindings=resolve_semantic_bindings(frame,Q3)
    # Force the earlier semantic-binding mistake without changing the frame's required semantic role.
    measure=binding_for(bindings,kind="measurement_field")
    measure["field"]="errorCode"
    measure["role_required"]="failure_category"
    measure["binding_id"]="SB-COARSE"
    measure["fingerprint"]="coarse"
    st["semantic_bindings"]=[measure]
    payload={"events":[
        {"errorCode":"AccessDenied","errorMessage":"not authorized CreateUser"},
        {"errorCode":"AccessDenied","errorMessage":"not authorized GetUser"},
        {"errorCode":"AccessDenied","errorMessage":"not authorized DeleteAccessKey"},
    ]}
    metrics={}
    result=detect_and_rebind(st,{"evidence_id":"E3"},payload,metrics)
    assert result is not None
    assert st["semantic_bindings"][0]["status"] == "INVALIDATED"
    assert any(b.get("field")=="errorMessage" and b.get("status")=="SUPPORTED" for b in st["semantic_bindings"])
    assert metrics["semantic_conflicts_detected"] == 1
    assert metrics["semantic_binding_invalidations"] == 1


def test_b2_question_shapes_map_to_reusable_primitives_not_answers():
    repo = Path('/mnt/data/bsides_repo/bsides-canberra26-copilot-master/labs/lab2/botsv3_b2_tests.json')
    if not repo.exists():
        return
    questions={k:v["question"] for k,v in json.loads(repo.read_text()).items()}
    frames={k:heuristic_frame(q) for k,q in questions.items()}
    assert "AGGREGATE" in frames["b2_q18_iam_key_errors"]["primitives"]
    assert frames["b2_q19_support_case_id"]["domain"] == "email"
    assert frames["b2_q20_secret_access_key"]["requires_reference_follow"] is True
    assert "DIRECT_EXTRACT" in frames["b2_q21_target_resource"]["primitives"]
    assert frames["b2_q22_user_agent"]["measure_role"] == "client_identifier"
    assert frames["b2_q23_ubuntu_codename"]["temporal_constraint"] == "earliest"
    assert frames["b2_q23_ubuntu_codename"]["requires_external_enrichment"] is True


def test_semantic_layer_source_has_no_benchmark_answers():
    texts=[]
    for p in Path('commander_agent/semantic').glob('*.py'):
        texts.append(p.read_text(encoding='utf-8'))
    joined='\n'.join(texts)
    forbidden = ['AKIA' + 'JOGCDXJ5NW5PXUPA', 'known benchmark codename', '5244' + '329601']
    assert all(value not in joined for value in forbidden)


def test_semantic_binding_blocks_precontract_metric_drift():
    from commander_agent.semantic.binding import semantic_binding_query_violations
    frame=heuristic_frame(Q3)
    bindings=resolve_semantic_bindings(frame,Q3)
    bad='index=botsv3 sourcetype=aws:cloudtrail | stats dc(errorCode) AS distinct_count BY userIdentity.accessKeyId'
    good='index=botsv3 sourcetype=aws:cloudtrail | stats dc(errorMessage) AS distinct_count BY userIdentity.accessKeyId'
    assert semantic_binding_query_violations(bad,bindings)
    assert semantic_binding_query_violations(good,bindings) == []
