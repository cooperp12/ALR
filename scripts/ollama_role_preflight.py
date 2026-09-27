from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from commander_agent.config import INVESTIGATOR_MODEL, SUPERVISOR_MODEL
from commander_agent.reasoning.local_ollama import structured_chat, message_content
from commander_agent.skills.structured_output_validation.scripts.schema import parse_model_object

SCHEMA = {
    "type": "object",
    "required": ["status"],
    "properties": {"status": {"type": "string", "enum": ["OK"]}},
}


def main():
    try:
        import ollama
        ollama.show(INVESTIGATOR_MODEL)
        ollama.show(SUPERVISOR_MODEL)
    except Exception as exc:
        print(f"Ollama model availability check FAILED: {type(exc).__name__}: {exc}")
        print(f"Required Investigator model: {INVESTIGATOR_MODEL}")
        print(f"Required Supervisor model  : {SUPERVISOR_MODEL}")
        return 1

    prompt = 'Return exactly this JSON object and nothing else: {"status":"OK"}'
    checks = [
        ("supervisor", SUPERVISOR_MODEL, False, 180),
        ("investigator", INVESTIGATOR_MODEL, "low", 500),
    ]
    for role, model, think, budget in checks:
        try:
            response = structured_chat(
                role=role,
                messages=[{"role": "user", "content": prompt}],
                schema=SCHEMA,
                purpose=f"setup_{role}_structured_smoke",
                num_ctx=2048 if role == "supervisor" else 4096,
                num_predict=budget,
                think=think,
            )
            ok, obj, errors = parse_model_object(message_content(response), SCHEMA)
            if not ok or obj.get("status") != "OK":
                print(f"{role.title()} structured-output smoke FAILED for {model}: {'; '.join(errors[:3])}")
                return 1
            print(f"{role.title()} model smoke PASS: {model}")
        except Exception as exc:
            print(f"{role.title()} model smoke FAILED for {model}: {type(exc).__name__}: {exc}")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
