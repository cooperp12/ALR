import json
from commander_agent.config import (
    OLLAMA_NUM_CTX,
    CONTEXT_TARGET_RATIO,
    CONTEXT_HARD_RATIO,
    APPROX_CHARS_PER_TOKEN,
    MAX_EVIDENCE_ITEMS_IN_CONTEXT,
    MAX_EVIDENCE_EXCERPT_CHARS,
    MAX_TIMELINE_CONTEXT_CHARS,
    MAX_PRIOR_VALIDATED_CONTEXT_CHARS,
)
from commander_agent.mcp.results import clip_text
from commander_agent.state.evidence import evidence_digest
from commander_agent.state.plan import plan_for_prompt
from commander_agent.state.semantic_state import compact_semantic_state


def estimate_tokens(text):
    if not text:
        return 0
    return max(1, int(len(str(text)) / max(APPROX_CHARS_PER_TOKEN, 1.0)))


def estimate_messages_tokens(messages):
    total = 0
    for message in messages:
        if isinstance(message, dict):
            total += estimate_tokens(message.get("content", "")) + 8
        else:
            total += estimate_tokens(getattr(message, "content", "")) + 8
    return total


def _compact_plan(plan):
    try:
        obj = json.loads(plan_for_prompt(plan))
    except Exception:
        return plan_for_prompt(plan)

    # Only include the most recent dynamic TODOs/history-relevant state.
    obj["dynamic_todos"] = obj.get("dynamic_todos", [])[-3:]
    return json.dumps(obj, ensure_ascii=False, indent=2)


def build_turn_context(
    question,
    plan,
    case_state,
    prior_context="",
    timeline_context="",
    strategy_guidance="",
    feedback="",
    system_prompt="",
):
    evidence = evidence_digest(
        case_state,
        max_items=MAX_EVIDENCE_ITEMS_IN_CONTEXT,
        excerpt_chars=MAX_EVIDENCE_EXCERPT_CHARS,
    )

    sections = [
        "QUESTION\n" + question,
        "CURRENT PLAN\n" + _compact_plan(plan),
    ]

    if strategy_guidance:
        sections.append("SKILL STRATEGY / QUERY SHAPE\n" + strategy_guidance)

    if prior_context:
        sections.append(
            "PRIOR VALIDATED CONTEXT\n"
            + clip_text(prior_context, MAX_PRIOR_VALIDATED_CONTEXT_CHARS)
        )

    if timeline_context:
        sections.append(
            "REUSABLE TIMELINE / CASE MAP\n"
            + clip_text(timeline_context, MAX_TIMELINE_CONTEXT_CHARS)
        )

    sections.append("CURRENT EVIDENCE\n" + (evidence or "[none yet]"))

    if feedback:
        sections.append("CHECK / REVIEW FEEDBACK\n" + feedback)

    sections.append(
        "NEXT ACTION\n"
        "Take one single-minded action. Prefer cached case/timeline evidence first. "
        "If a Splunk search is necessary, follow the active skill query contracts. "
        "When evidence already proves the answer, provide a concise candidate answer."
    )

    content = "\n\n".join(sections)

    target_tokens = int(OLLAMA_NUM_CTX * CONTEXT_TARGET_RATIO)
    hard_tokens = int(OLLAMA_NUM_CTX * CONTEXT_HARD_RATIO)
    system_tokens = estimate_tokens(system_prompt)
    available_for_user = max(2200, target_tokens - system_tokens)
    estimated = estimate_tokens(content)
    compacted = False

    if estimated > available_for_user:
        # First trim low-priority reusable context. Current evidence and current plan
        # remain because they are the working state of the investigation.
        prior_context = clip_text(prior_context, 900)
        timeline_context = clip_text(timeline_context, 1100)
        evidence = evidence_digest(case_state, max_items=5, excerpt_chars=650)
        sections = [
            "QUESTION\n" + question,
            "CURRENT PLAN\n" + _compact_plan(plan),
        ]
        if strategy_guidance:
            sections.append("SKILL STRATEGY / QUERY SHAPE\n" + clip_text(strategy_guidance, 1600))
        if prior_context:
            sections.append("PRIOR VALIDATED CONTEXT\n" + prior_context)
        if timeline_context:
            sections.append("REUSABLE TIMELINE / CASE MAP\n" + timeline_context)
        sections.append("CURRENT EVIDENCE\n" + (evidence or "[none yet]"))
        if feedback:
            sections.append("CHECK / REVIEW FEEDBACK\n" + clip_text(feedback, 1500))
        sections.append(
            "NEXT ACTION\nTake one single-minded action; do not replay old raw results."
        )
        content = "\n\n".join(sections)
        compacted = True

    total_est = system_tokens + estimate_tokens(content)

    if total_est > hard_tokens:
        # Hard fallback: keep only question, plan, latest evidence and feedback.
        evidence = evidence_digest(case_state, max_items=3, excerpt_chars=450)
        content = (
            "QUESTION\n" + question
            + "\n\nCURRENT PLAN\n" + clip_text(_compact_plan(plan), 1800)
            + "\n\nCURRENT EVIDENCE\n" + (evidence or "[none yet]")
            + ("\n\nFEEDBACK\n" + clip_text(feedback, 900) if feedback else "")
            + "\n\nNEXT ACTION\nTake one focused action only."
        )
        compacted = True
        total_est = system_tokens + estimate_tokens(content)

    return content, {
        "estimated_prompt_tokens": total_est,
        "context_compacted": compacted,
        "target_tokens": target_tokens,
        "hard_tokens": hard_tokens,
    }
