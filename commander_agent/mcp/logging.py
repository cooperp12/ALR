import json
from datetime import datetime
from commander_agent.config import FULL_RESULT_LOG
def append_full_result_log(round_number, tool_name, arguments, result_text):
    try:
        with open(FULL_RESULT_LOG, "a", encoding="utf-8") as fh:
            fh.write("\n")
            fh.write("=" * 90 + "\n")
            fh.write(
                f"{datetime.now().isoformat()} "
                f"ROUND {round_number} TOOL {tool_name}\n"
            )
            fh.write("=" * 90 + "\n")
            fh.write("ARGUMENTS:\n")
            fh.write(json.dumps(arguments, indent=2, default=str))
            fh.write("\n\nFULL RESULT:\n")
            fh.write(result_text)
            fh.write("\n")

    except Exception as exc:
        print(f"[Could not write full-result log: {exc}]")
