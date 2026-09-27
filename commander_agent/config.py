import json
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
CREDENTIALS_FILE = CONFIG_DIR / "credentials.json"


def _read_json(path: Path):
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _resolve_path(value, default=None):
    raw = value if value not in (None, "") else default
    if raw in (None, ""):
        return None
    p = Path(str(raw)).expanduser()
    if not p.is_absolute():
        p = (PROJECT_ROOT / p).resolve()
    return p


_SETTINGS = _read_json(SETTINGS_FILE)
_CREDENTIALS = _read_json(CREDENTIALS_FILE)

# -----------------------------------------------------------------------------
# Local model roles / context
# -----------------------------------------------------------------------------
# ALR legacy compatibility keeps the current local role split:
#   Investigator = gpt-oss:20b for harder semantic/query reasoning.
#   Supervisor   = granite4.2:8b for compact routing/validation decisions.
# MODEL remains an alias for the Investigator so older investigation code keeps
# using the stronger worker without duplicating configuration.
INVESTIGATOR_MODEL = os.getenv(
    "OLLAMA_INVESTIGATOR_MODEL",
    str(_SETTINGS.get("ollama_investigator_model", "gpt-oss:20b")),
)
SUPERVISOR_MODEL = os.getenv(
    "OLLAMA_SUPERVISOR_MODEL",
    str(_SETTINGS.get("ollama_supervisor_model", "granite4.2:8b")),
)
MODEL = INVESTIGATOR_MODEL

INVESTIGATOR_NUM_CTX = int(os.getenv(
    "OLLAMA_INVESTIGATOR_NUM_CTX",
    str(_SETTINGS.get("ollama_investigator_num_ctx", 8192)),
))
SUPERVISOR_NUM_CTX = int(os.getenv(
    "OLLAMA_SUPERVISOR_NUM_CTX",
    str(_SETTINGS.get("ollama_supervisor_num_ctx", 8192)),
))
OLLAMA_NUM_CTX = INVESTIGATOR_NUM_CTX

# A 20B gpt-oss model plus an 8B Granite model cannot both comfortably remain
# resident on a 16 GB GPU. Default to unload-after-call role switching. Users can
# override this if their hardware has more VRAM.
OLLAMA_ROLE_KEEP_ALIVE = os.getenv(
    "OLLAMA_ROLE_KEEP_ALIVE",
    str(_SETTINGS.get("ollama_role_keep_alive", "0s")),
)
INVESTIGATOR_THINK = os.getenv(
    "OLLAMA_INVESTIGATOR_THINK",
    str(_SETTINGS.get("ollama_investigator_think", "low")),
)
SUPERVISOR_THINK = os.getenv(
    "OLLAMA_SUPERVISOR_THINK",
    str(_SETTINGS.get("ollama_supervisor_think", "false")),
)

INVESTIGATOR_STRUCTURED_NUM_PREDICT = int(os.getenv(
    "OLLAMA_INVESTIGATOR_NUM_PREDICT",
    str(_SETTINGS.get("ollama_investigator_num_predict", 1800)),
))
INVESTIGATOR_RETRY_NUM_PREDICT = int(os.getenv(
    "OLLAMA_INVESTIGATOR_RETRY_NUM_PREDICT",
    str(_SETTINGS.get("ollama_investigator_retry_num_predict", 2800)),
))
SUPERVISOR_STRUCTURED_NUM_PREDICT = int(os.getenv(
    "OLLAMA_SUPERVISOR_NUM_PREDICT",
    str(_SETTINGS.get("ollama_supervisor_num_predict", 900)),
))
SUPERVISOR_RETRY_NUM_PREDICT = int(os.getenv(
    "OLLAMA_SUPERVISOR_RETRY_NUM_PREDICT",
    str(_SETTINGS.get("ollama_supervisor_retry_num_predict", 1400)),
))

CONTEXT_TARGET_RATIO = float(os.getenv("CONTEXT_TARGET_RATIO", "0.68"))
CONTEXT_HARD_RATIO = float(os.getenv("CONTEXT_HARD_RATIO", "0.78"))
APPROX_CHARS_PER_TOKEN = float(os.getenv("APPROX_CHARS_PER_TOKEN", "4.0"))
MAX_EVIDENCE_ITEMS_IN_CONTEXT = int(os.getenv("MAX_EVIDENCE_ITEMS_IN_CONTEXT", "8"))
MAX_EVIDENCE_EXCERPT_CHARS = int(os.getenv("MAX_EVIDENCE_EXCERPT_CHARS", "1000"))
MAX_TIMELINE_CONTEXT_CHARS = int(os.getenv("MAX_TIMELINE_CONTEXT_CHARS", "3200"))
MAX_PRIOR_VALIDATED_CONTEXT_CHARS = int(os.getenv("MAX_PRIOR_VALIDATED_CONTEXT_CHARS", "2200"))
MAX_EVENTS_FOR_MODEL = int(os.getenv("MAX_EVENTS_FOR_MODEL", "16"))
MAX_EVENT_CHARS = int(os.getenv("MAX_EVENT_CHARS", "1100"))
MAX_TOTAL_MODEL_RESULT_CHARS = int(os.getenv("MAX_TOTAL_MODEL_RESULT_CHARS", "9000"))

# -----------------------------------------------------------------------------
# Versioned state
# -----------------------------------------------------------------------------
WORK_VERSION = "36.0-authoritative-codename"
CACHE_SCHEMA_VERSION = 10
VALIDATION_POLICY_VERSION = 12
SKILL_CONTRACT_VERSION = 14
TIMELINE_SCHEMA_VERSION = 6
STRUCTURED_OUTPUT_SCHEMA_VERSION = 13

FULL_RESULT_LOG = "ALR_mcp_full.log"
SCORE_FILE = "ALR_scores.json"
PLAN_FILE = "ALR_plan.json"
QUERY_CACHE_FILE = "ALR_query_cache.json"
VALIDATED_CACHE_FILE = "ALR_validated_cache.json"
EVIDENCE_LEDGER_FILE = "ALR_evidence.jsonl"
TIMELINE_LEDGER_FILE = "ALR_timeline.jsonl"
TIMELINE_ANCHOR_FILE = "ALR_timeline_anchors.json"
RELATIONSHIP_LEDGER_FILE = "ALR_relationships.jsonl"
QUERY_FAILURE_LEDGER_FILE = "ALR_query_failures.jsonl"
EVAL_LEDGER_FILE = "ALR_eval_runs.jsonl"
SEMANTIC_FINDINGS_FILE = "ALR_semantic_findings.jsonl"
OBSERVATION_LEDGER_FILE = "ALR_observations.jsonl"
MEASUREMENT_CONTRACT_LEDGER_FILE = "ALR_measurement_contracts.jsonl"
CANDIDATE_DERIVATION_LEDGER_FILE = "ALR_candidate_derivations.jsonl"
CANDIDATE_VERIFICATION_LEDGER_FILE = "ALR_candidate_verifications.jsonl"
EXTRACTION_CONTRACT_LEDGER_FILE = "ALR_extraction_contracts.jsonl"
EXTRACTION_DERIVATION_LEDGER_FILE = "ALR_extraction_derivations.jsonl"
CASE_STATE_DB_FILE = "ALR_case_state.sqlite"

INVESTIGATION_FRAME_LEDGER_FILE = "ALR_investigation_frames.jsonl"
SEMANTIC_BINDING_LEDGER_FILE = "ALR_semantic_bindings.jsonl"
SEMANTIC_CONFLICT_LEDGER_FILE = "ALR_semantic_conflicts.jsonl"
ARTIFACT_DIR = str(_resolve_path(os.getenv("ALR legacy compatibility_ARTIFACT_DIR", _SETTINGS.get("artifact_dir", "ALR_artifacts"))))

MAX_SEARCH_EXECUTIONS = int(os.getenv("MAX_SEARCH_EXECUTIONS", "15"))
MAX_AGENT_ROUNDS = int(os.getenv("MAX_AGENT_ROUNDS", "8"))
MIN_VALIDATION_SUPPORT = int(os.getenv("MIN_VALIDATION_SUPPORT", "2"))
MAX_SEMANTIC_RECOVERY_STEPS = int(os.getenv("MAX_SEMANTIC_RECOVERY_STEPS", "12"))
MAX_SEMANTIC_REVIEWS_PER_EVIDENCE_VERSION = int(os.getenv("MAX_SEMANTIC_REVIEWS_PER_EVIDENCE_VERSION", "12"))
MAX_SEMANTIC_FORMAT_REPAIRS = int(os.getenv("MAX_SEMANTIC_FORMAT_REPAIRS", "1"))
MAX_SEMANTIC_ACTION_REPAIRS = int(os.getenv("MAX_SEMANTIC_ACTION_REPAIRS", "1"))
MAX_AGENT_TURNS_WITHOUT_NEW_EVIDENCE = int(os.getenv("MAX_AGENT_TURNS_WITHOUT_NEW_EVIDENCE", "1"))
TEMPORAL_MAX_REDUCED_ROWS = int(os.getenv("TEMPORAL_MAX_REDUCED_ROWS", str(_SETTINGS.get("temporal_max_reduced_rows", 10000))))

MODEL_ROUTER_ON_CONFIDENT_HEURISTIC = _bool(
    os.getenv("ALR legacy compatibility_MODEL_ROUTER_ON_CONFIDENT_HEURISTIC", "0")
)
MODEL_PLANNER_ON_HIGH_CONFIDENCE_STRATEGY = _bool(
    os.getenv("ALR legacy compatibility_MODEL_PLANNER_ON_HIGH_CONFIDENCE_STRATEGY", "0")
)
ENABLE_BOOTSTRAP_QUERIES = _bool(os.getenv("ALR legacy compatibility_BOOTSTRAP_QUERIES", "1"), True)
FORCE_FRESH = _bool(os.getenv("ALR legacy compatibility_FORCE_FRESH", "0"))

# -----------------------------------------------------------------------------
# Optional OpenAI reasoning assist
# -----------------------------------------------------------------------------
# Disabled by default. No API key and no user-specific key path is embedded.
# To enable it, set openai_reasoning_enabled=true in config/settings.json and
# provide openai_key_file in config/credentials.json (or OPENAI_KEY_FILE env var).
ENABLE_OPENAI_REASONING = _bool(
    os.getenv(
        "ALR legacy compatibility_OPENAI_REASONING",
        str(_SETTINGS.get("openai_reasoning_enabled", False)),
    )
)
OPENAI_KEY_FILE = _resolve_path(
    os.getenv("OPENAI_KEY_FILE", _CREDENTIALS.get("openai_key_file", ""))
)
OPENAI_RESPONSES_URL = os.getenv(
    "OPENAI_RESPONSES_URL",
    str(_SETTINGS.get("openai_responses_url", "https://api.openai.com/v1/responses")),
)
OPENAI_LUNA_MODEL = os.getenv(
    "OPENAI_LUNA_MODEL", str(_SETTINGS.get("openai_luna_model", "gpt-5.6-luna"))
)
OPENAI_TERRA_MODEL = os.getenv(
    "OPENAI_TERRA_MODEL", str(_SETTINGS.get("openai_terra_model", "gpt-5.6-terra"))
)
OPENAI_TIMEOUT_SECONDS = float(os.getenv("OPENAI_TIMEOUT_SECONDS", "30"))
OPENAI_PLAN_EFFORT = os.getenv("OPENAI_PLAN_EFFORT", "low")
OPENAI_VALIDATION_EFFORT = os.getenv("OPENAI_VALIDATION_EFFORT", "medium")
OPENAI_CONFLICT_EFFORT = os.getenv("OPENAI_CONFLICT_EFFORT", "high")

# -----------------------------------------------------------------------------
# Splunk MCP
# -----------------------------------------------------------------------------
# ALR legacy compatibility uses portable, package-relative defaults. setup-ALR creates these.
MCP_PYTHON = str(_resolve_path(
    os.getenv("SPLUNK_MCP_PYTHON", _SETTINGS.get("mcp_python", ".venv/Scripts/python.exe"))
))
MCP_SERVER = str(_resolve_path(
    os.getenv(
        "SPLUNK_MCP_SERVER",
        _SETTINGS.get("mcp_server", "runtime/splunk-mcp-server2/python/server.py"),
    )
))
MCP_CWD = str(_resolve_path(
    os.getenv(
        "SPLUNK_MCP_CWD",
        _SETTINGS.get("mcp_cwd", "runtime/splunk-mcp-server2/python"),
    )
))

SPLUNK_HOST = os.getenv("SPLUNK_HOST", str(_SETTINGS.get("splunk_host", "127.0.0.1")))
SPLUNK_PORT = os.getenv("SPLUNK_PORT", str(_SETTINGS.get("splunk_port", "8089")))
SPLUNK_USERNAME = os.getenv("SPLUNK_USERNAME", str(_CREDENTIALS.get("splunk_username", ""))).strip()
SPLUNK_PASSWORD = os.getenv("SPLUNK_PASSWORD", str(_CREDENTIALS.get("splunk_password", "")))
SPLUNK_VERIFY_SSL = os.getenv(
    "SPLUNK_VERIFY_SSL", str(_SETTINGS.get("splunk_verify_ssl", "false"))
).strip().lower()
SPLUNK_CONTAINER_NAME = os.getenv(
    "SPLUNK_CONTAINER_NAME", str(_SETTINGS.get("splunk_container_name", "splunk-botsv3"))
)
SPLUNK_PREFLIGHT_QUERY = os.getenv(
    "SPLUNK_PREFLIGHT_QUERY", "index=botsv3 | stats count AS botsv3_count"
)

BOTSV3_EARLIEST = "0"
BOTSV3_LATEST = "now"
