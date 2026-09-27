from __future__ import annotations

from typing import Any
from functools import lru_cache
import json
import math

from commander_agent.config import (
    INVESTIGATOR_MODEL,
    SUPERVISOR_MODEL,
    INVESTIGATOR_NUM_CTX,
    SUPERVISOR_NUM_CTX,
    INVESTIGATOR_THINK,
    SUPERVISOR_THINK,
    OLLAMA_ROLE_KEEP_ALIVE,
    INVESTIGATOR_STRUCTURED_NUM_PREDICT,
    INVESTIGATOR_RETRY_NUM_PREDICT,
    SUPERVISOR_STRUCTURED_NUM_PREDICT,
    SUPERVISOR_RETRY_NUM_PREDICT,
)


def _ollama_chat(**kwargs):
    import ollama
    return ollama.chat(**kwargs)


@lru_cache(maxsize=8)
def thinking_controls(model):
    try:
        import ollama
        info = ollama.show(model)
        values = _field(_field(info, "thinking", {}), "values", [])
        if values:
            return tuple(values)
    except Exception:
        pass
    # Older servers do not publish thinking metadata. GPT-OSS has named levels.
    return ("low", "medium", "high") if "gpt-oss" in model.lower() else ()


def supported_think(model, requested, retry=False):
    values = thinking_controls(model)
    if not values:
        return requested  # Preserve configured behaviour when metadata is unavailable.
    if retry and False in values:
        return False
    if (retry or requested not in values) and "low" in values:
        return "low"
    return requested if requested in values else values[0]


def _context_options(messages, schema, cfg, first_budget, retry_budget):
    # Conservative estimate, not an exact tokenizer count. Never silently truncate evidence.
    chars = len(json.dumps(messages, ensure_ascii=False)) + len(json.dumps(schema))
    estimate = math.ceil(chars / 3) + 256
    required = estimate + max(first_budget, retry_budget) + 256
    capacity = int(cfg)
    if required > capacity:
        raise ValueError(f"Structured prompt needs approximately {required} tokens including output reserve; "
                         f"context is {capacity}. Reduce evidence/state before retrying.")
    return estimate


def _field(obj: Any, name: str, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def message_content(response: Any) -> str:
    message = _field(response, "message")
    if isinstance(message, dict):
        return str(message.get("content") or "")
    return str(getattr(message, "content", "") or "")


def message_thinking(response: Any) -> str:
    message = _field(response, "message")
    if isinstance(message, dict):
        return str(message.get("thinking") or "")
    return str(getattr(message, "thinking", "") or "")


def done_reason(response: Any) -> str:
    return str(_field(response, "done_reason", "") or "").strip().lower()


def prompt_tokens(response: Any) -> int:
    try:
        return int(_field(response, "prompt_eval_count", 0) or 0)
    except Exception:
        return 0


def output_tokens(response: Any) -> int:
    try:
        return int(_field(response, "eval_count", 0) or 0)
    except Exception:
        return 0


def add_usage(metrics: dict | None, response: Any, role: str | None = None):
    if metrics is None:
        return
    p = prompt_tokens(response)
    o = output_tokens(response)
    metrics["ollama_prompt_tokens"] = metrics.get("ollama_prompt_tokens", 0) + p
    metrics["ollama_output_tokens"] = metrics.get("ollama_output_tokens", 0) + o
    if role:
        metrics[f"ollama_{role}_prompt_tokens"] = metrics.get(f"ollama_{role}_prompt_tokens", 0) + p
        metrics[f"ollama_{role}_output_tokens"] = metrics.get(f"ollama_{role}_output_tokens", 0) + o
    for field in ("total_duration", "load_duration", "prompt_eval_duration", "eval_duration"):
        metrics[f"ollama_{field}_seconds"] = metrics.get(f"ollama_{field}_seconds", 0) + float(_field(response, field, 0) or 0) / 1e9


def _normalise_think(value):
    if isinstance(value, bool) or value is None:
        return value
    text = str(value).strip().lower()
    if text in {"", "none"}:
        return None
    if text in {"0", "false", "off", "no"}:
        return False
    if text in {"1", "true", "on", "yes"}:
        return True
    return text


def role_settings(role: str) -> dict[str, Any]:
    if role == "supervisor":
        return {
            "model": SUPERVISOR_MODEL,
            "num_ctx": SUPERVISOR_NUM_CTX,
            "think": _normalise_think(SUPERVISOR_THINK),
            "num_predict": SUPERVISOR_STRUCTURED_NUM_PREDICT,
            "retry_num_predict": SUPERVISOR_RETRY_NUM_PREDICT,
        }
    return {
        "model": INVESTIGATOR_MODEL,
        "num_ctx": INVESTIGATOR_NUM_CTX,
        "think": _normalise_think(INVESTIGATOR_THINK),
        "num_predict": INVESTIGATOR_STRUCTURED_NUM_PREDICT,
        "retry_num_predict": INVESTIGATOR_RETRY_NUM_PREDICT,
    }


def _chat_with_optional_think(kwargs: dict[str, Any]):
    try:
        return _ollama_chat(**kwargs)
    except TypeError as exc:
        # Older Ollama Python clients may not expose the think argument. Preserve
        # compatibility rather than failing the whole local role call.
        if "think" in kwargs and "think" in str(exc).lower():
            retry = dict(kwargs)
            retry.pop("think", None)
            return _ollama_chat(**retry)
        raise


def structured_response_looks_truncated(response: Any, num_predict: int) -> bool:
    content = message_content(response).strip()
    thinking = message_thinking(response).strip()
    reason = done_reason(response)
    out = output_tokens(response)
    if reason in {"length", "max_tokens", "token_limit"}:
        return True
    if not content and num_predict and out >= max(1, int(num_predict * 0.95)):
        return True
    return False


def structured_chat(
    *,
    role: str,
    messages,
    schema,
    metrics: dict | None = None,
    purpose: str = "structured",
    num_ctx: int | None = None,
    num_predict: int | None = None,
    think=None,
    temperature: float = 0,
    keep_alive: str | None = None,
):
    """Run one role-specific structured call with one truncation recovery attempt.

    Use model-supported thinking controls and reserve context for the retry response.
    """
    cfg = role_settings(role)
    first_budget = int(num_predict or cfg["num_predict"])
    think_value = supported_think(cfg["model"], cfg["think"] if think is None else _normalise_think(think))
    retry_budget = max(first_budget + 256, int(cfg["retry_num_predict"]))
    context = int(num_ctx or cfg["num_ctx"])
    # Small repair/preflight overrides must still reserve enough room for a retry.
    context = max(context, int(cfg["num_ctx"]))
    _context_options(messages, schema, context, first_budget, retry_budget)
    kwargs = {
        "model": cfg["model"],
        "messages": messages,
        "format": schema,
        "options": {
            "num_ctx": context,
            "temperature": temperature,
            "num_predict": first_budget,
        },
        "keep_alive": OLLAMA_ROLE_KEEP_ALIVE if keep_alive is None else keep_alive,
    }
    if think_value is not None:
        kwargs["think"] = think_value

    response = _chat_with_optional_think(kwargs)
    add_usage(metrics, response, role=role)
    def telemetry(resp, budget, thinking):
        if metrics is not None:
            metrics.setdefault("ollama_calls", []).append({"role": role, "purpose": purpose,
                "content_chars": len(message_content(resp)), "thinking_chars": len(message_thinking(resp)),
                "output_tokens": output_tokens(resp), "done_reason": done_reason(resp),
                "budget": budget, "think": thinking})
    telemetry(response, first_budget, think_value)

    if not structured_response_looks_truncated(response, first_budget):
        return response

    if metrics is not None:
        metrics["ollama_truncation_detected"] = metrics.get("ollama_truncation_detected", 0) + 1
        metrics["ollama_truncation_retries"] = metrics.get("ollama_truncation_retries", 0) + 1
    print(
        f"[Ollama {role} output appears truncated before final JSON for {purpose}; "
        "retrying once with model-supported thinking controls and a larger output allowance.]"
    )

    retry_kwargs = dict(kwargs)
    retry_kwargs["think"] = supported_think(cfg["model"], think_value, retry=True)
    retry_options = dict(kwargs["options"])
    retry_options["num_predict"] = retry_budget
    retry_kwargs["options"] = retry_options
    retry_response = _chat_with_optional_think(retry_kwargs)
    add_usage(metrics, retry_response, role=role)
    telemetry(retry_response, retry_budget, retry_kwargs["think"])
    if structured_response_looks_truncated(retry_response, retry_budget):
        raise RuntimeError(f"MODEL_OUTPUT_TRUNCATED: {role}/{purpose} exhausted its bounded retry")
    return retry_response


def role_chat(
    *,
    role: str,
    messages,
    tools=None,
    metrics: dict | None = None,
    options: dict | None = None,
    think=None,
    keep_alive: str | None = None,
):
    """Run a non-schema role call, including tool-calling Investigator turns."""
    cfg = role_settings(role)
    merged = {"num_ctx": cfg["num_ctx"], "temperature": 0}
    if options:
        merged.update(options)
    kwargs = {
        "model": cfg["model"],
        "messages": messages,
        "options": merged,
        "keep_alive": OLLAMA_ROLE_KEEP_ALIVE if keep_alive is None else keep_alive,
    }
    if tools is not None:
        kwargs["tools"] = tools
    think_value = supported_think(cfg["model"], cfg["think"] if think is None else _normalise_think(think))
    if think_value is not None:
        kwargs["think"] = think_value
    response = _chat_with_optional_think(kwargs)
    add_usage(metrics, response, role=role)
    return response
