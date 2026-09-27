from __future__ import annotations

from commander_agent.config import (
    ENABLE_OPENAI_REASONING,
    OPENAI_LUNA_MODEL,
    OPENAI_TERRA_MODEL,
    OPENAI_PLAN_EFFORT,
    OPENAI_VALIDATION_EFFORT,
    OPENAI_CONFLICT_EFFORT,
)
from commander_agent.reasoning.openai_api import read_api_key, create_response


class ReasoningBroker:
    """Optional remote reasoning assist with hard fallback to local Ollama role models.

    The broker tests the API before use. If preflight or any later request fails,
    remote reasoning is disabled for the rest of the process rather than repeatedly
    stalling the investigation.
    """

    def __init__(self):
        self.enabled = bool(ENABLE_OPENAI_REASONING)
        self.api_key = None
        self.failure_reason = ""
        self.preflight_passed = False

    @property
    def available(self):
        return self.enabled and self.preflight_passed and bool(self.api_key)

    def disable(self, reason):
        self.enabled = False
        self.failure_reason = str(reason)

    def preflight(self):
        if not self.enabled:
            return False, "OpenAI reasoning disabled by configuration; using local Ollama only."

        key, error = read_api_key()
        if not key:
            self.disable(error)
            return False, f"OpenAI reasoning unavailable ({error}); using local Ollama only."

        self.api_key = key
        try:
            # Test BOTH configured reasoning models before allowing either one into
            # the investigation. If either model/key/API path fails, remote reasoning
            # is disabled for the entire run so we do not repeatedly stall later.
            for model in (OPENAI_LUNA_MODEL, OPENAI_TERRA_MODEL):
                result = create_response(
                    api_key=self.api_key,
                    model=model,
                    prompt="Reply with exactly: OK",
                    effort="none",
                    max_output_tokens=32,
                )
                if "OK" not in (result.get("text") or "").upper():
                    raise RuntimeError(f"{model} preflight response did not contain OK")
        except Exception as exc:
            self.api_key = None
            self.disable(exc)
            return False, f"OpenAI reasoning preflight failed ({exc}); using local Ollama only."

        self.preflight_passed = True
        return True, (
            f"OpenAI reasoning ready ({OPENAI_LUNA_MODEL} fast / {OPENAI_TERRA_MODEL} deep)."
        )

    def _call(self, model, prompt, effort, max_output_tokens, metrics=None, purpose="reasoning"):
        if not self.available:
            return None
        try:
            result = create_response(
                api_key=self.api_key,
                model=model,
                prompt=prompt,
                effort=effort,
                max_output_tokens=max_output_tokens,
            )
            if metrics is not None:
                metrics["openai_reasoning_calls"] = metrics.get("openai_reasoning_calls", 0) + 1
                metrics[f"openai_{purpose}_calls"] = metrics.get(f"openai_{purpose}_calls", 0) + 1
                usage = result.get("usage") or {}
                for source_key, target_key in (
                    ("input_tokens", "openai_input_tokens"),
                    ("output_tokens", "openai_output_tokens"),
                    ("total_tokens", "openai_total_tokens"),
                ):
                    if isinstance(usage.get(source_key), int):
                        metrics[target_key] = metrics.get(target_key, 0) + usage[source_key]
            return result.get("text", "")
        except Exception as exc:
            if metrics is not None:
                metrics["openai_reasoning_failures"] = metrics.get("openai_reasoning_failures", 0) + 1
            self.disable(exc)
            print(f"[OpenAI reasoning disabled after failure: {type(exc).__name__}: {exc}]")
            return None

    def fast(self, prompt, metrics=None, purpose="fast", max_output_tokens=1200, effort=None):
        return self._call(
            OPENAI_LUNA_MODEL,
            prompt,
            effort or OPENAI_PLAN_EFFORT,
            max_output_tokens,
            metrics=metrics,
            purpose=purpose,
        )

    def deep(self, prompt, metrics=None, purpose="validation", max_output_tokens=1800, effort=None):
        return self._call(
            OPENAI_TERRA_MODEL,
            prompt,
            effort or OPENAI_VALIDATION_EFFORT,
            max_output_tokens,
            metrics=metrics,
            purpose=purpose,
        )

    def conflict(self, prompt, metrics=None, max_output_tokens=2000):
        return self._call(
            OPENAI_TERRA_MODEL,
            prompt,
            OPENAI_CONFLICT_EFFORT,
            max_output_tokens,
            metrics=metrics,
            purpose="conflict",
        )
