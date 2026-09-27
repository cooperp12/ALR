import json
import re
from pathlib import Path


def test_benchmark_question_files_do_not_contain_plaintext_expected_fields():
    root = Path(__file__).parents[1]
    for name in ("botsv3_q1_q3_q6_benchmark.json", "botsv3_q1_q3_benchmark.json"):
        data = json.loads((root / "benchmarks" / name).read_text(encoding="utf-8"))
        for test in data.values():
            assert "expected_output" not in test
            assert "expected_patterns" not in test


def test_score_oracle_contains_only_sha256_digests():
    root = Path(__file__).parents[1]
    data = json.loads((root / "benchmarks" / "score_oracle.sha256.json").read_text(encoding="utf-8"))
    assert set(data) == {"Q1", "Q2", "Q3", "Q6"}
    assert all(re.fullmatch(r"[0-9a-f]{64}", value or "") for value in data.values())


def test_q6_runtime_has_no_local_release_codename_catalogue_and_uses_shared_timeline():
    root = Path(__file__).parents[1]
    source = (root / "commander_agent" / "core" / "q6_ubuntu_launch.py").read_text(encoding="utf-8")
    assert "UBUNTU_RELEASES" not in source
    assert "resolve_ubuntu_codename" in source
    assert "append_timeline_events" in source
    assert "timeline_related" in source
    assert "timeline_map" in source
