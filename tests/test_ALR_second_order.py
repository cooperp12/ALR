import json
import random
import string
from pathlib import Path

from commander_agent.core import q6_ubuntu_launch as q6
from commander_agent.state.sequence import build_validated_sequence_state, sequence_values
from commander_agent.evaluation import isolation


def _key(prefix, rng):
    alphabet = string.ascii_uppercase + string.digits
    return prefix + ''.join(rng.choice(alphabet) for _ in range(16))


def test_generic_sequence_state_promotes_typed_validated_facts_only():
    key = "AKIAABCDEFGHIJKLMNOP"
    state = build_validated_sequence_state([
        {"qid":"A", "question":"What is the support case ID?", "answer":"1234567890", "metrics":{"final_validation_status":"validated","validated_support_ids":["E1"]}},
        {"qid":"B", "question":"What is the user agent?", "answer":"SyntheticClient/4.2", "metrics":{"final_validation_status":"validated"}},
        {"qid":"C", "question":"Which AWS access key?", "answer":key, "metrics":{"final_validation_status":"validated"}},
        {"qid":"D", "question":"ignored", "answer":"bad", "metrics":{"final_validation_status":"not reached"}},
    ])
    assert sequence_values(state, "support_case_id") == ["1234567890"]
    assert sequence_values(state, "user_agent") == ["SyntheticClient/4.2"]
    assert sequence_values(state, "aws_access_key") == [key]
    assert state["validated_qids"] == ["A","B","C"]


def test_dynamic_temporal_windows_expand_from_observed_identity_span():
    identity = {"scope_basis_start_epoch":1000.0,"scope_basis_end_epoch":1010.0,"scope_basis_event_count":2}
    w5 = q6._temporal_window(identity, 5)
    w60 = q6._temporal_window(identity, 60)
    assert w5["start_epoch"] == 700.0 and w5["end_epoch"] == 1310.0
    assert w60["start_epoch"] < w5["start_epoch"] and w60["end_epoch"] > w5["end_epoch"]


def test_randomised_identity_expansion_is_literal_independent():
    rng = random.Random(3401)
    for i in range(40):
        original = _key("AKIA", rng)
        derived = _key("ASIA", rng)
        user = "user_" + ''.join(rng.choice(string.ascii_lowercase) for _ in range(10))
        row = {"eventTime":f"2018-01-01T00:{i%60:02d}:00Z","eventName":"GetSessionToken","eventSource":"sts.amazonaws.com",
               "access_key":original,"iam_user":user,"derived_access_key":derived,"errorCode":""}
        graph = q6._derive_identity_graph([row], original)
        assert graph["status"] == "resolved"
        assert graph["iam_user"] == user
        assert graph["all_access_keys"] == [original, derived]
        assert graph["scope_basis_start_epoch"] == graph["scope_basis_end_epoch"]


def test_relationship_runtime_has_confidence_and_provenance_contract():
    source = (Path(__file__).parents[1] / "commander_agent" / "skills" / "bidirectional_timeframe" / "scripts" / "timeline_engine.py").read_text(encoding="utf-8")
    for token in ("confidence_score", "sufficient_for_identity", "provenance", "explicit_same_event_credential_derivation"):
        assert token in source


def test_enrichment_hierarchy_is_ordered_and_requires_corroboration():
    source = (Path(__file__).parents[1] / "commander_agent" / "core" / "q6_ubuntu_launch.py").read_text(encoding="utf-8")
    order = [source.index(x) for x in ("_canonical_locator_lookup", "_canonical_stream_lookup", "_authoritative_search_lookup", "_corroborated_search_lookup")]
    assert order == sorted(order)
    assert "len(domains) >= 2" in source


def test_benchmark_isolation_targets_mutable_state_not_oracle():
    names = {str(p) for p in isolation._runtime_targets()}
    assert "ALR_scores.json" in names
    assert "ALR_validated_cache.json" in names
    assert not any("score_oracle" in x for x in names)
    assert not any("credentials" in x for x in names)


def test_stage_trace_contract_exists():
    source = (Path(__file__).parents[1] / "commander_agent" / "core" / "q6_ubuntu_launch.py").read_text(encoding="utf-8")
    for stage in ("sequence_context","identity_retrieval","timeline_relationships","temporal_scope","anomaly_review","first_event","chronology_verification","ami_release_enrichment","codename_enrichment","answer_contract"):
        assert f'"{stage}"' in source


def test_relationship_edges_carry_actionable_identity_confidence(tmp_path):
    from commander_agent.skills.bidirectional_timeframe.scripts import timeline_engine as te
    te.TIMELINE_LEDGER_FILE = str(tmp_path / "timeline.jsonl")
    te.TIMELINE_ANCHOR_FILE = str(tmp_path / "anchors.json")
    te.RELATIONSHIP_LEDGER_FILE = str(tmp_path / "relationships.jsonl")
    original = "AKIAABCDEFGHIJKLMNOP"
    derived = "ASIAABCDEFGHIJKLMNOP"
    case = {"case_id":"rel-case","anchor":None}
    evidence = {"evidence_id":"E-ID","source":"splunk","tool":"search_oneshot","method_class":"timeline_identity_expansion","query":"index=x"}
    te.append_timeline_events(case, evidence, [{
        "_time":"2018-01-01T00:00:00Z", "eventName":"GetSessionToken",
        "accessKeyId":original, "derived_access_key":derived, "userName":"alice",
        "sourceIPAddress":"203.0.113.4",
    }])
    rels = te.timeline_related(original, case_id="rel-case")
    cred = next(r for r in rels if r["relation"] == "RELATED_CREDENTIAL" and r["to"] == derived)
    assert cred["confidence"] == "high"
    assert cred["confidence_score"] >= 0.9
    assert cred["sufficient_for_identity"] is True
    assert cred["provenance_records"][0]["evidence_id"] == "E-ID"
    ip = next(r for r in rels if r["relation"] == "SEEN_FROM")
    assert ip["sufficient_for_identity"] is False


def test_benchmark_isolation_archives_prior_mutable_state(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    Path("ALR_scores.json").write_text("{}")
    Path("ALR_validated_cache.json").write_text("{}")
    Path("ALR_timeline.jsonl").write_text("old\n")
    Path("benchmarks").mkdir()
    Path("benchmarks/score_oracle.sha256.json").write_text('{"QX":"' + 'a'*64 + '"}')
    manifest = isolation.prepare_benchmark_isolation()
    assert manifest["fresh_state"] is True
    assert not Path("ALR_scores.json").exists()
    assert not Path("ALR_validated_cache.json").exists()
    assert not Path("ALR_timeline.jsonl").exists()
    assert Path("ALR_benchmark_run.json").exists()
    assert Path("benchmarks/score_oracle.sha256.json").exists()
    archive = Path(manifest["archive_directory"])
    assert (archive / "ALR_scores.json").exists()
    assert (archive / "ALR_timeline.jsonl").exists()


def test_enrichment_fallback_order_executes_by_tier(monkeypatch):
    calls=[]
    monkeypatch.setattr(q6, "_canonical_locator_lookup", lambda a,r: calls.append("locator") or None)
    monkeypatch.setattr(q6, "_canonical_stream_lookup", lambda a,r: calls.append("stream") or None)
    monkeypatch.setattr(q6, "_authoritative_search_lookup", lambda a,r: calls.append("authoritative") or None)
    monkeypatch.setattr(q6, "_corroborated_search_lookup", lambda a,r: calls.append("corroborated") or {"release":{"version":"99.99","series":"synthetic"},"source_tier":"corroborated_web"})
    out=q6.resolve_ubuntu_ami("ami-12345678","eu-test-1")
    assert calls == ["locator","stream","authoritative","corroborated"]
    assert out["source_tier"] == "corroborated_web"


def test_codename_direct_urls_are_constructed_from_evidenced_version_only():
    urls = q6._official_codename_urls("99.99")
    assert urls == [
        "https://releases.ubuntu.com/99.99/",
        "https://cloud-images.ubuntu.com/releases/99.99/release/",
        "https://ubuntu.com/99-99",
    ]
    assert q6._official_codename_urls("not-a-version") == []


def test_codename_prefers_direct_official_release_pages_without_search(monkeypatch):
    fetched = []
    def fake_fetch(url):
        fetched.append(url)
        return url, "Ubuntu 99.99 LTS (Example Eland)"
    monkeypatch.setattr(q6, "_fetch_evidence_blocks", fake_fetch)
    monkeypatch.setattr(q6, "web_search_public", lambda query: (_ for _ in ()).throw(AssertionError("search fallback should not run")))
    out = q6.resolve_ubuntu_codename({"version":"99.99", "series":""})
    assert out["codename"] == "Example Eland"
    assert out["source_tier"] == "official_direct_release_page"
    assert len(out["corroborating_sources"]) == 3
    assert fetched == q6._official_codename_urls("99.99")


def test_conflicting_direct_official_codenames_do_not_silently_resolve(monkeypatch):
    urls = q6._official_codename_urls("99.99")
    pages = {
        urls[0]: "Ubuntu 99.99 LTS (Example Eland)",
        urls[1]: "Ubuntu Server 99.99 LTS (Different Dingo)",
        urls[2]: "Ubuntu 99.99 LTS (Example Eland)",
    }
    monkeypatch.setattr(q6, "_fetch_evidence_blocks", lambda url: (url, pages[url]))
    assert q6._official_codename_direct_lookup("99.99") is None
