import json
import re
from datetime import datetime


from commander_agent.config import (
    PLAN_FILE,
    WORK_VERSION,
    MODEL_PLANNER_ON_HIGH_CONFIDENCE_STRATEGY,
)
from commander_agent.state.cache import question_cache_key
from commander_agent.state.io import json_write_atomic
from commander_agent.skills.contracts import infer_investigation_type
from commander_agent.skills.structured_output_validation.scripts.schema import (
    PLANNER_SCHEMA,
    parse_model_object,
)
from commander_agent.reasoning.local_ollama import structured_chat, message_content


def parse_json_object_loose(value):
    """Compatibility helper; use contract validation for trusted planner/reviewer data."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    match = re.search(r"\{.*\}", value, flags=re.DOTALL)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def make_case_id(question):
    prefix = question_cache_key(question)[:10]
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    return f"{prefix}-{stamp}"


def _deterministic_strategy(question, active_skills, query_strategy):
    kind = query_strategy.get("investigation_type") or infer_investigation_type(question, active_skills)
    guidance = query_strategy.get("guidance", "")
    if kind == "timeline":
        primary = (
            "Reuse the local case map first. If the anchor event is not already mapped, "
            "run one targeted search to establish it, then set evidence-backed T0."
        )
        check = (
            "Inspect BEFORE and AFTER windows around T0 and pivot through entity relationships; "
            "widen only when the map remains incomplete."
        )
    else:
        primary = (
            "Execute the selected skill's reducing primary query and preserve the result as evidence."
            if query_strategy.get("primary_query")
            else "Run the cheapest targeted search that directly establishes the requested fact."
        )
        check = (
            "Cross-check with the selected skill's materially different validation query."
            if query_strategy.get("check_query")
            else "Cross-check the candidate with a materially different evidence path."
        )

    return {
        "investigation_type": kind,
        "answer_contract": "Answer exactly what the user asked, using evidence.",
        "primary_strategy": primary,
        "check_strategy": check,
        "review_checks": [
            "Every material condition in the question is satisfied.",
            "The requested value comes from the same matching evidence/result.",
            "The check uses a materially different method, not a cosmetic query variant.",
        ],
        "timeline_needed": kind == "timeline",
        "external_enrichment_needed": kind == "external_enrichment",
        "stop_condition": "Stop after independent consistent evidence survives review.",
        "skill_guidance": guidance,
    }


def planner_strategy(
    question,
    active_skills,
    prior_context,
    query_strategy=None,
    metrics=None,
    reasoner=None,
):
    query_strategy = query_strategy or {}
    if query_strategy.get("confidence") == "high" and not MODEL_PLANNER_ON_HIGH_CONFIDENCE_STRATEGY:
        if metrics is not None:
            metrics["deterministic_planner_hits"] = metrics.get("deterministic_planner_hits", 0) + 1
        return _deterministic_strategy(question, active_skills, query_strategy)

    prompt = f'''
You are planning a BOTSv3 SOC investigation. Do NOT answer the question.
Create a compact plan using PLAN -> DO -> CHECK -> REVIEW -> CONTINUE/STOP.
Keep every objective single-minded. CHECK must use a materially different evidence
method. Retry failures/non-findings, not successful work. If timeline work is needed,
reuse the case map before querying Splunk and establish an evidence-backed T0.

Loaded skills: {", ".join(active_skills)}
Skill strategy: {json.dumps(query_strategy, ensure_ascii=False)}
Prior validated context: {prior_context or "[none]"}

Return ONLY JSON with:
{{
  "investigation_type": "direct_lookup|aggregation|chronological|timeline|multi_step|external_enrichment",
  "answer_contract": "...",
  "primary_strategy": "...",
  "check_strategy": "...",
  "review_checks": ["..."],
  "timeline_needed": false,
  "external_enrichment_needed": false,
  "stop_condition": "..."
}}
QUESTION: {question}
'''

    parsed = None
    if reasoner is not None and reasoner.available:
        text = reasoner.fast(
            prompt,
            metrics=metrics,
            purpose="planner",
            max_output_tokens=900,
            effort="low",
        )
        if text:
            ok, obj, errors = parse_model_object(text, PLANNER_SCHEMA)
            if ok:
                parsed = obj
            elif metrics is not None:
                metrics["structured_output_rejections"] = metrics.get("structured_output_rejections", 0) + 1
                print("[Remote planner JSON rejected: " + "; ".join(errors[:3]) + "]")

    if parsed is None:
        try:
            if metrics is not None:
                metrics["model_planner_calls"] = metrics.get("model_planner_calls", 0) + 1
            response = structured_chat(
                role="supervisor",
                messages=[{"role": "user", "content": prompt}],
                schema=PLANNER_SCHEMA,
                metrics=metrics,
                purpose="planner",
                num_ctx=4096,
                think=False,
            )
            ok, obj, errors = parse_model_object(message_content(response), PLANNER_SCHEMA)
            if ok:
                parsed = obj
            else:
                if metrics is not None:
                    metrics["structured_output_rejections"] = metrics.get("structured_output_rejections", 0) + 1
                print("[Local planner JSON rejected: " + "; ".join(errors[:3]) + "]")
        except Exception as exc:
            print(f"[Planner warning: {type(exc).__name__}: {exc}]")

    if parsed:
        inferred = infer_investigation_type(question, active_skills)
        if inferred in {"aggregation", "timeline", "chronological", "external_enrichment"}:
            parsed["investigation_type"] = inferred
            parsed["timeline_needed"] = inferred == "timeline"
            parsed["external_enrichment_needed"] = inferred == "external_enrichment"
        return parsed

    if metrics is not None:
        metrics["deterministic_planner_hits"] = metrics.get("deterministic_planner_hits", 0) + 1
    return _deterministic_strategy(question, active_skills, query_strategy)


def build_plan(
    question,
    active_skills,
    prior_context,
    query_strategy=None,
    metrics=None,
    reasoner=None,
    investigation_frame=None,
    semantic_bindings=None,
):
    strategy = planner_strategy(
        question,
        active_skills,
        prior_context,
        query_strategy=query_strategy,
        metrics=metrics,
        reasoner=reasoner,
    )
    case_id = make_case_id(question)

    dynamic_todos = []
    if strategy.get("investigation_type") == "timeline":
        dynamic_todos.extend([
            {
                "id": "T1",
                "objective": "Reuse existing case-map evidence before new Splunk work",
                "reason": "Map-first investigation avoids rebuilding known history.",
                "status": "pending",
                "evidence_ids": [],
            },
            {
                "id": "T2",
                "objective": "Establish an evidence-backed T0 anchor",
                "reason": "Before/after positioning requires a validated event timestamp.",
                "status": "pending",
                "evidence_ids": [],
            },
            {
                "id": "T3",
                "objective": "Inspect +/-30 minute BEFORE/AFTER windows and entity relationships",
                "reason": "Start narrow; widen only if incomplete.",
                "status": "pending",
                "evidence_ids": [],
            },
        ])
    else:
        if query_strategy and query_strategy.get("primary_query"):
            dynamic_todos.append({
                "id": "T1",
                "objective": "Execute primary skill query",
                "reason": query_strategy.get("guidance", "Selected skill contract"),
                "status": "pending",
                "evidence_ids": [],
            })
        if query_strategy and query_strategy.get("check_query"):
            dynamic_todos.append({
                "id": f"T{len(dynamic_todos)+1}",
                "objective": "Execute independent cross-check query",
                "reason": "Cross-validation requires a materially different method.",
                "status": "pending",
                "evidence_ids": [],
            })

    dynamic_todos.append({
        "id": f"T{len(dynamic_todos)+1}",
        "objective": "Audit evidence against the question and decide stop/continue",
        "reason": "No final answer is validated without review.",
        "status": "pending",
        "evidence_ids": [],
    })

    plan = {
        "work_version": WORK_VERSION,
        "case_id": case_id,
        "question": question,
        "created": datetime.now().isoformat(),
        "strategy": strategy,
        "query_strategy": query_strategy or {},
        "investigation_frame": investigation_frame or {},
        "semantic_bindings": list(semantic_bindings or []),
        "source_contract": dict((query_strategy or {}).get("source_contract") or {}),
        "extraction_contract": dict((query_strategy or {}).get("extraction_contract") or {}),
        "phases": [
            {"id": "P1", "phase": "PLAN", "objective": "Scope the question and choose the cheapest reliable route.", "status": "completed", "note": strategy.get("investigation_type", ""), "evidence_ids": []},
            {"id": "P2", "phase": "DO", "objective": strategy.get("primary_strategy", ""), "status": "in_progress", "note": "", "evidence_ids": []},
            {"id": "P3", "phase": "CHECK", "objective": strategy.get("check_strategy", ""), "status": "pending", "note": "", "evidence_ids": []},
            {"id": "P4", "phase": "REVIEW", "objective": "Audit: " + "; ".join(strategy.get("review_checks", [])), "status": "pending", "note": "", "evidence_ids": []},
            {"id": "P5", "phase": "CONTINUE/STOP", "objective": strategy.get("stop_condition", ""), "status": "pending", "note": "", "evidence_ids": []},
        ],
        "dynamic_todos": dynamic_todos,
        "history": [],
    }
    save_plan(plan)
    return plan


def save_plan(plan):
    try:
        json_write_atomic(PLAN_FILE, plan)
    except Exception as exc:
        print(f"[Plan save warning: {type(exc).__name__}: {exc}]")


def phase_entry(plan, phase):
    for item in plan.get("phases", []):
        if item.get("phase") == phase:
            return item
    return None


def update_plan_phase(plan, phase, status, note="", evidence_ids=None):
    item = phase_entry(plan, phase)
    if not item:
        return
    item["status"] = status
    if note:
        item["note"] = note
    if evidence_ids:
        existing = item.setdefault("evidence_ids", [])
        for evidence_id in evidence_ids:
            if evidence_id not in existing:
                existing.append(evidence_id)
    plan.setdefault("history", []).append({
        "time": datetime.now().isoformat(),
        "phase": phase,
        "status": status,
        "note": note,
        "evidence_ids": list(evidence_ids or []),
    })
    save_plan(plan)


def add_dynamic_todo(plan, objective, reason):
    if not objective:
        return
    todos = plan.setdefault("dynamic_todos", [])
    todos.append({
        "id": f"T{len(todos)+1}",
        "objective": objective,
        "reason": reason,
        "status": "pending",
        "evidence_ids": [],
        "created": datetime.now().isoformat(),
    })
    save_plan(plan)


def complete_next_todo(plan, evidence_ids=None, note=""):
    for todo in plan.get("dynamic_todos", []):
        if todo.get("status") == "pending":
            todo["status"] = "completed"
            todo["completed"] = datetime.now().isoformat()
            todo["evidence_ids"] = list(evidence_ids or [])
            if note:
                todo["note"] = note
            save_plan(plan)
            return todo
    return None


def complete_todo_by_text(plan, contains, evidence_ids=None, note=""):
    needle = str(contains).lower()
    for todo in plan.get("dynamic_todos", []):
        if todo.get("status") == "pending" and needle in str(todo.get("objective", "")).lower():
            todo["status"] = "completed"
            todo["completed"] = datetime.now().isoformat()
            todo["evidence_ids"] = list(evidence_ids or [])
            if note:
                todo["note"] = note
            save_plan(plan)
            return todo
    return None


def print_plan(plan):
    print()
    print("=" * 70)
    print("DURABLE INVESTIGATION PLAN")
    print("=" * 70)
    print(f"Case         : {plan.get('case_id')}")
    print("Type         : " + str(plan.get("strategy", {}).get("investigation_type", "")))
    print("Answer target: " + str(plan.get("strategy", {}).get("answer_contract", "")))
    print()
    for item in plan.get("phases", []):
        print(f"[{item.get('status','pending').upper():11}] {item.get('phase')}: {item.get('objective')}")
    if plan.get("dynamic_todos"):
        print("\nTODOs:")
        for todo in plan["dynamic_todos"]:
            print(f"  [{todo.get('status','pending').upper():9}] {todo.get('id')}: {todo.get('objective')}")


def plan_for_prompt(plan):
    concise = {
        "case_id": plan.get("case_id"),
        "strategy": plan.get("strategy"),
        "phases": [
            {"phase": p.get("phase"), "objective": p.get("objective"), "status": p.get("status"), "note": p.get("note")}
            for p in plan.get("phases", [])
        ],
        "dynamic_todos": plan.get("dynamic_todos", [])[-6:],
    }
    return json.dumps(concise, indent=2, ensure_ascii=False)
