import json
import re
from datetime import datetime

from commander_agent.config import (
    CACHE_SCHEMA_VERSION,
    VALIDATION_POLICY_VERSION,
    SKILL_CONTRACT_VERSION,
    QUERY_CACHE_FILE,
    VALIDATED_CACHE_FILE,
    FORCE_FRESH,
    WORK_VERSION,
)
from commander_agent.state.io import json_load_file, json_write_atomic, stable_hash
from commander_agent.skills.structured_output_validation.scripts.schema import (
    validate_search_arguments,
    validate_search_result_text,
)


def canonical_question_text(value):
    value = (value or "").lower()
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def question_cache_key(question):
    return stable_hash(
        {
            "dataset": "botsv3",
            "question": canonical_question_text(question),
            "validation_policy": VALIDATION_POLICY_VERSION,
            "skill_contract": SKILL_CONTRACT_VERSION,
        }
    )


def normalise_spl_for_cache(query):
    return re.sub(r"\s+", " ", (query or "").strip())


def query_cache_signature(tool_name, arguments):
    important = {
        "tool": tool_name,
        "query": normalise_spl_for_cache(arguments.get("query", "")),
        "earliest_time": arguments.get("earliest_time"),
        "latest_time": arguments.get("latest_time"),
        "max_count": arguments.get("max_count"),
        "output_format": arguments.get("output_format"),
        "cache_schema": CACHE_SCHEMA_VERSION,
        "skill_contract": SKILL_CONTRACT_VERSION,
    }
    return stable_hash(important)


def load_query_cache():
    cache = json_load_file(QUERY_CACHE_FILE, {})
    return cache if isinstance(cache, dict) else {}


def get_query_cache_entry(signature):
    entry = load_query_cache().get(signature)
    if not isinstance(entry, dict):
        return None
    if entry.get("cache_schema") != CACHE_SCHEMA_VERSION:
        return None
    if entry.get("skill_contract") != SKILL_CONTRACT_VERSION:
        return None
    validation = validate_search_result_text(entry.get("model_result", ""))
    if not validation.get("ok"):
        return None
    return entry


def put_query_cache_entry(signature, tool_name, arguments, model_result):
    args_ok, canonical_args, arg_errors = validate_search_arguments(arguments)
    result_validation = validate_search_result_text(model_result)
    if not args_ok:
        return False, f"query cache rejected arguments: {'; '.join(arg_errors)}"
    if not result_validation.get("ok"):
        return False, f"query cache rejected result: {'; '.join(result_validation.get('errors', []))}"

    cache = load_query_cache()
    cache[signature] = {
        "cache_schema": CACHE_SCHEMA_VERSION,
        "skill_contract": SKILL_CONTRACT_VERSION,
        "tool": tool_name,
        "arguments": canonical_args,
        "model_result": model_result,
        "event_count": result_event_count(model_result),
        "created": datetime.now().isoformat(),
        "hits": 0,
    }
    json_write_atomic(QUERY_CACHE_FILE, cache)
    return True, ""


def bump_query_cache_hit(signature):
    cache = load_query_cache()
    entry = cache.get(signature)
    if not isinstance(entry, dict):
        return
    entry["hits"] = int(entry.get("hits", 0)) + 1
    entry["last_hit"] = datetime.now().isoformat()
    json_write_atomic(QUERY_CACHE_FILE, cache)


def result_event_count(model_result):
    try:
        parsed = json.loads(model_result)
        if isinstance(parsed, dict):
            value = parsed.get("event_count")
            if value is None and isinstance(parsed.get("events"), list):
                return len(parsed["events"])
            if value is None and isinstance(parsed.get("results"), list):
                return len(parsed["results"])
            return int(value) if value is not None else None
    except Exception:
        return None
    return None


def load_validated_cache():
    data = json_load_file(VALIDATED_CACHE_FILE, {})
    return data if isinstance(data, dict) else {}


def get_validated_answer(question):
    if FORCE_FRESH:
        return None
    entry = load_validated_cache().get(question_cache_key(question))
    if not isinstance(entry, dict):
        return None
    if entry.get("status") != "validated":
        return None
    if entry.get("validation_policy") != VALIDATION_POLICY_VERSION:
        return None
    if entry.get("skill_contract") != SKILL_CONTRACT_VERSION:
        return None
    return entry


def save_validated_answer(question, answer, plan, review, metrics, active_skills):
    cache = load_validated_cache()
    cache[question_cache_key(question)] = {
        "status": "validated",
        "validation_policy": VALIDATION_POLICY_VERSION,
        "skill_contract": SKILL_CONTRACT_VERSION,
        "work_version": WORK_VERSION,
        "question": question,
        "answer": answer,
        "case_id": plan.get("case_id"),
        "support_ids": review.get("support_ids", []),
        "confidence": review.get("confidence"),
        "validation_summary": review.get("summary"),
        "skills": list(active_skills),
        "search_calls": metrics.get("search_calls"),
        "tool_calls": metrics.get("tool_calls"),
        "updated": datetime.now().isoformat(),
    }
    json_write_atomic(VALIDATED_CACHE_FILE, cache)


def invalidate_validated_answer(question, reason):
    cache = load_validated_cache()
    key = question_cache_key(question)
    entry = cache.get(key)
    if not isinstance(entry, dict):
        return
    entry["status"] = "rejected_by_eval"
    entry["rejected_reason"] = reason
    entry["rejected_at"] = datetime.now().isoformat()
    json_write_atomic(VALIDATED_CACHE_FILE, cache)
