import json
from commander_agent.skills.registry import SKILLS


def mcp_tools_to_ollama(mcp_tools):
    allowed = {"search_oneshot", "search_export"}
    ollama_tools = []

    for tool in mcp_tools:
        if tool.name not in allowed:
            continue
        schema = tool.inputSchema or {"type": "object", "properties": {}}
        ollama_tools.append(
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": schema,
                },
            }
        )

    ollama_tools.extend(
        [
            {
                "type": "function",
                "function": {
                    "name": "query_timeline",
                    "description": (
                        "Query the reusable local timeline/case map before running a new Splunk search. "
                        "Can filter by terms, entity, case, and absolute time range."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "terms": {"type": "array", "items": {"type": "string"}},
                            "case_id": {"type": "string"},
                            "entity": {"type": "string"},
                            "start_time": {"type": "string"},
                            "end_time": {"type": "string"},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "set_timeline_anchor",
                    "description": (
                        "Set/version evidence-backed T0 for the current case. The timestamp must already "
                        "exist in stored timeline evidence; guessed timestamps are rejected."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "event_time": {"type": "string"},
                            "evidence_id": {"type": "string"},
                            "reason": {"type": "string"},
                            "entity": {"type": "string"},
                        },
                        "required": ["event_time", "reason"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "timeline_before",
                    "description": "Return case-map events before current T0 within a minute window.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "case_id": {"type": "string"},
                            "minutes": {"type": "number", "minimum": 1, "maximum": 1440},
                            "entity": {"type": "string"},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                        },
                        "required": ["case_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "timeline_after",
                    "description": "Return case-map events after current T0 within a minute window.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "case_id": {"type": "string"},
                            "minutes": {"type": "number", "minimum": 1, "maximum": 1440},
                            "entity": {"type": "string"},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                        },
                        "required": ["case_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "timeline_between",
                    "description": "Return chronologically sorted case-map events between absolute timestamps.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "start_time": {"type": "string"},
                            "end_time": {"type": "string"},
                            "case_id": {"type": "string"},
                            "entity": {"type": "string"},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                        },
                        "required": ["start_time", "end_time"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "timeline_entities",
                    "description": "List frequent normalized entities already present in the case map.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "case_id": {"type": "string"},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "timeline_related",
                    "description": (
                        "Pivot through persistent entity relationships such as access-key->user, "
                        "access-key->source-IP, actor->action, and action->resource."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "entity": {"type": "string"},
                            "case_id": {"type": "string"},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                        },
                        "required": ["entity"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "timeline_map",
                    "description": "Return a compact event/entity/relationship map for a case or entity.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "case_id": {"type": "string"},
                            "entity": {"type": "string"},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "run_temporal_analysis",
                    "description": (
                        "Run deterministic temporal/trend analysis over the CURRENT scoped event population. "
                        "The harness preserves the scope, reduces in Splunk, then computes features with Polars/NumPy. "
                        "Do not use this before a scope contract exists."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "group_field": {"type": ["string", "null"]},
                            "value_field": {"type": ["string", "null"]},
                            "time_field": {"type": "string"},
                            "measures": {
                                "type": "array",
                                "items": {"type": "string", "enum": [
                                    "event_count", "distinct_count", "cumulative_distinct_count", "new_distinct_count"
                                ]}
                            },
                            "comparison_feature": {"type": ["string", "null"]}
                        },
                        "required": ["group_field", "value_field", "time_field", "measures", "comparison_feature"]
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "load_skill",
                    "description": "Load an additional reusable investigation skill when evidence shows another capability is needed.",
                    "parameters": {
                        "type": "object",
                        "properties": {"skill_name": {"type": "string", "enum": sorted(SKILLS.keys())}},
                        "required": ["skill_name"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": "Search the public web only after Splunk provides a concrete artefact needing external enrichment.",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "fetch_url",
                    "description": "Fetch a public HTTP/HTTPS URL discovered during the investigation.",
                    "parameters": {
                        "type": "object",
                        "properties": {"url": {"type": "string"}},
                        "required": ["url"],
                    },
                },
            },
        ]
    )
    return ollama_tools


def tool_result_to_text(result):
    parts = []
    for item in result.content:
        if hasattr(item, "text"):
            parts.append(item.text)
        else:
            parts.append(str(item))
    return "\n".join(parts) if parts else str(result)


def get_tool_name(tool_call):
    try:
        return tool_call.function.name
    except AttributeError:
        return tool_call["function"]["name"]


def get_tool_arguments(tool_call):
    try:
        args = tool_call.function.arguments
    except AttributeError:
        args = tool_call["function"]["arguments"]
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return dict(args or {})
