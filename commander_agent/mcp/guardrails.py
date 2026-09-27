import json
import re
from commander_agent.config import BOTSV3_EARLIEST,BOTSV3_LATEST
def contains_recent_relative_time(query):
    if not isinstance(query, str):
        return False

    patterns = [
        r"\bearliest\s*=\s*-\d+[smhdw]\b",
        r"\blatest\s*=\s*-\d+[smhdw]\b",
        r"\bearliest\s*=\s*@d\b",
        r"\bearliest\s*=\s*now\b",
    ]

    return any(
        re.search(pattern, query, flags=re.IGNORECASE)
        for pattern in patterns
    )

def is_botsv3_query(query):
    if not isinstance(query, str):
        return False

    return bool(
        re.search(
            r"\bindex\s*=\s*[\"']?botsv3[\"']?",
            query,
            flags=re.IGNORECASE,
        )
    )

def strip_mcp_time_parameters_from_spl(query):
    if not isinstance(query, str):
        return query, []

    removed = []

    pattern = re.compile(
        r'''(?ix)
        (?<!\S)
        (earliest_time|latest_time)
        \s*=\s*
        ("[^"]*"|'[^']*'|[^\s|]+)
        '''
    )

    def repl(match):
        removed.append(match.group(0))
        return ""

    cleaned = pattern.sub(repl, query)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\s+\|", " |", cleaned)

    return cleaned.strip(), removed

def is_broad_raw_sample(query):
    """
    Block only generic raw sampling. Aggregations such as
    index=botsv3 | stats count by sourcetype remain allowed.
    """
    if not isinstance(query, str):
        return False

    cleaned = query.strip()
    cleaned = re.sub(r"(?i)^search\s+", "", cleaned).strip()

    if re.fullmatch(
        r'''(?ix)
        index\s*=\s*["']?botsv3["']?
        ''',
        cleaned,
    ):
        return True

    if re.fullmatch(
        r'''(?ix)
        index\s*=\s*["']?botsv3["']?
        \s*\|\s*head\s+\d+
        ''',
        cleaned,
    ):
        return True

    return False

def apply_search_guardrail(tool_name, arguments):
    if tool_name not in ("search_oneshot", "search_export"):
        return arguments, None

    guarded = dict(arguments)
    query = guarded.get("query", "")

    if not is_botsv3_query(query):
        return guarded, None

    cleaned, removed = strip_mcp_time_parameters_from_spl(query)
    guarded["query"] = cleaned

    if contains_recent_relative_time(cleaned):
        return (
            guarded,
            json.dumps(
                {
                    "guardrail": "Historical BOTSv3 time rule blocked this search.",
                    "blocked_query": cleaned,
                    "required_fix": (
                        "Remove recent relative-time modifiers. The wrapper applies "
                        "earliest_time=0 and latest_time=now."
                    ),
                },
                indent=2,
            ),
        )

    if is_broad_raw_sample(cleaned):
        return (
            guarded,
            json.dumps(
                {
                    "guardrail": "Broad raw BOTSv3 sample blocked.",
                    "blocked_query": cleaned,
                    "required_fix": (
                        "Use the question and loaded skills to target a sourcetype, "
                        "identifier, or action, or discover sourcetypes with an aggregate "
                        "such as stats count by sourcetype. Do not sample arbitrary raw events."
                    ),
                },
                indent=2,
            ),
        )

    guarded["earliest_time"] = BOTSV3_EARLIEST
    guarded["latest_time"] = BOTSV3_LATEST

    if tool_name == "search_oneshot":
        requested = guarded.get("max_count", 20)

        try:
            requested = int(requested)
        except (TypeError, ValueError):
            requested = 20

        guarded["max_count"] = min(requested, 50)

    return guarded, None

