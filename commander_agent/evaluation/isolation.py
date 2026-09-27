from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
from typing import Any

from commander_agent import config


def _runtime_targets() -> list[Path]:
    names = [
        config.SCORE_FILE, config.PLAN_FILE, config.QUERY_CACHE_FILE,
        config.VALIDATED_CACHE_FILE, config.EVIDENCE_LEDGER_FILE,
        config.TIMELINE_LEDGER_FILE, config.TIMELINE_ANCHOR_FILE,
        config.RELATIONSHIP_LEDGER_FILE, config.QUERY_FAILURE_LEDGER_FILE,
        config.EVAL_LEDGER_FILE, config.SEMANTIC_FINDINGS_FILE,
        config.OBSERVATION_LEDGER_FILE, config.MEASUREMENT_CONTRACT_LEDGER_FILE,
        config.CANDIDATE_DERIVATION_LEDGER_FILE, config.CANDIDATE_VERIFICATION_LEDGER_FILE,
        config.EXTRACTION_CONTRACT_LEDGER_FILE, config.EXTRACTION_DERIVATION_LEDGER_FILE,
        config.CASE_STATE_DB_FILE, config.CASE_STATE_DB_FILE + "-wal", config.CASE_STATE_DB_FILE + "-shm",
        config.CASE_STATE_DB_FILE + "-journal", config.INVESTIGATION_FRAME_LEDGER_FILE,
        config.SEMANTIC_BINDING_LEDGER_FILE, config.SEMANTIC_CONFLICT_LEDGER_FILE,
        config.FULL_RESULT_LOG,
        "ALR_q6_resolution.jsonl", "ALR_q6_stage_trace.json",
        "ALR_sequence_summary.json", "ALR_benchmark_run.json",
    ]
    out = []
    for name in names:
        p = Path(name)
        if p not in out:
            out.append(p)
    return out


def prepare_benchmark_isolation() -> dict[str, Any]:
    """Archive prior mutable runtime state before a packaged benchmark run.

    Source, credentials, settings, benchmark questions, and the isolated score
    oracle are never moved.  This makes repeated option-1 runs fresh while
    preserving prior state for inspection instead of deleting it.
    """
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    archive = Path("ALR_benchmark_archive") / run_id
    moved = []
    for path in _runtime_targets():
        if not path.exists():
            continue
        archive.mkdir(parents=True, exist_ok=True)
        target = archive / path.name
        shutil.move(str(path), str(target))
        moved.append({"from": str(path), "to": str(target)})

    artifact_dir = Path(config.ARTIFACT_DIR)
    if artifact_dir.exists():
        archive.mkdir(parents=True, exist_ok=True)
        target = archive / artifact_dir.name
        shutil.move(str(artifact_dir), str(target))
        moved.append({"from": str(artifact_dir), "to": str(target)})

    manifest = {
        "benchmark_run_id": run_id,
        "fresh_state": True,
        "archived_items": moved,
        "archive_directory": str(archive) if moved else None,
        "created_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    Path("ALR_benchmark_run.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest
