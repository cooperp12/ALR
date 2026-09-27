import json
import re
from pathlib import Path


from commander_agent.config import MODEL_ROUTER_ON_CONFIDENT_HEURISTIC
from commander_agent.core.prompts import BASE_SYSTEM_PROMPT
from commander_agent.skills.structured_output_validation.scripts.schema import parse_json_text
from commander_agent.reasoning.local_ollama import role_chat

SKILLS_ROOT = Path(__file__).resolve().parent
BASELINE_SKILLS = [
    "botsv3_environment",
    "botsv3_historical",
    "splunk_query_efficiency",
    "evidence_verification",
    "structured_output_validation",
]


def _strip_frontmatter(text):
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            return parts[2].lstrip()
    return text


def load_registry():
    out = {}
    for d in SKILLS_ROOT.iterdir():
        if not d.is_dir() or d.name.startswith("__"):
            continue
        meta = d / "metadata.json"
        skill = d / "SKILL.md"
        if not meta.exists() or not skill.exists():
            continue
        try:
            m = json.loads(meta.read_text(encoding="utf-8"))
            m["path"] = str(skill)
            out[d.name] = m
        except Exception:
            continue
    return out


SKILLS = load_registry()


def load_skill_instructions(name):
    skill = SKILLS.get(name)
    if not skill:
        return ""
    return _strip_frontmatter(Path(skill["path"]).read_text(encoding="utf-8")).strip()


def skill_catalog_text():
    lines = []
    for name, skill in SKILLS.items():
        if name in BASELINE_SKILLS or skill.get("role") == "control":
            continue
        lines.append(f"- {name}: {skill.get('description', '')}")
    return "\n".join(lines)


def parse_skill_names(text):
    ok, parsed, _ = parse_json_text(text or "", expected_type=list, allow_embedded=True)
    if ok:
        return [
            item for item in parsed
            if isinstance(item, str) and item in SKILLS and item not in BASELINE_SKILLS
        ]

    found = []
    for name in SKILLS:
        if name not in BASELINE_SKILLS and re.search(rf"\b{re.escape(name)}\b", text or "", flags=re.IGNORECASE):
            found.append(name)
    return found


def heuristic_skills(question):
    """Semantic capability routing only; no Q-number or expected-answer mapping."""
    low = (question or "").lower()
    chosen = []

    def add(name):
        if name in SKILLS and name not in chosen:
            chosen.append(name)

    notification_direct = ("support case" in low or "notification" in low) and not any(
        term in low for term in ("user agent", "create a key", "runinstances", "cloud image", "distinct errors")
    )
    if any(term in low for term in (
        "aws", "iam", "access key", "ec2", "cloud image", "ami", "user agent", "runinstances",
    )) and not notification_direct:
        add("aws_cloudtrail")

    if any(term in low for term in ("distinct", "unique", "different errors", "most errors")):
        add("spl_distinct_count")

    if any(term in low for term in ("email", "notification", "support case", "message", "repository")):
        add("email_investigation")

    if any(term in low for term in (
        "requestparameters", "specific resource", "target resource", "create a key", "createaccesskey",
    )):
        add("json_nested_fields")

    if any(term in low for term in ("first attempt", "first event", "earliest", "initial attempt")):
        add("chronological_first_event")

    if any(term in low for term in (
        "github", "external code repository", "url", "link", "codename", "ami", "cloud image",
        "look up", "lookup", "research",
    )) and "support case" not in low:
        add("external_enrichment")

    if any(term in low for term in (
        "ami", "cloud image", "ubuntu codename", "operating system version", "os version",
    )):
        add("ami_release_resolution")

    if any(term in low for term in (
        "compromised iam user", "derived credential", "temporary credential",
        "getsessiontoken", "sts token", "same compromised user",
    )):
        add("cloudtrail_identity_expansion")

    if any(term in low for term in (
        "over time", "trend", "spike", "burst", "increase", "decrease",
        "rate", "periodic", "frequency over time", "change point", "time series",
    )):
        add("temporal_pattern_analysis")

    if any(term in low for term in (
        "before and after", "before/after", "what happened before", "what happened after",
        "what happened next", "leading up to", "lead up to", "following the event",
        "following activity", "follow-on", "surrounding activity", "around this event",
        "around the event", "timeline", "trace the compromise", "campaign progression",
        "blast radius", "precursor", "lateral movement", "sequence of events",
    )):
        add("bidirectional_timeframe")

    return chosen


def select_skills(question, metrics=None, reasoner=None):
    heuristic_selected = heuristic_skills(question)

    if heuristic_selected and not MODEL_ROUTER_ON_CONFIDENT_HEURISTIC:
        if metrics is not None:
            metrics["heuristic_router_hits"] = metrics.get("heuristic_router_hits", 0) + 1
        return heuristic_selected[:6]

    router_prompt = f'''
You are a skill router for a Splunk BOTSv3 investigation.
Choose between 0 and 5 EXTRA skills that materially help answer the question.
Baseline environment, historical-search, efficient-SPL, evidence-verification, and
structured-output-validation skills are already loaded.

Use bidirectional_timeframe only for genuine temporal-context questions.
Do not answer the investigation question.

Available extra skills:
{skill_catalog_text()}

Return ONLY a JSON array of skill names.

QUESTION:
{question}
'''

    model_selected = []
    remote_text = None
    if reasoner is not None and reasoner.available:
        remote_text = reasoner.fast(
            router_prompt,
            metrics=metrics,
            purpose="router",
            max_output_tokens=350,
            effort="low",
        )
        if remote_text:
            model_selected = parse_skill_names(remote_text)

    if not model_selected:
        try:
            if metrics is not None:
                metrics["model_router_calls"] = metrics.get("model_router_calls", 0) + 1
            response = role_chat(
                role="supervisor",
                messages=[{"role": "user", "content": router_prompt}],
                metrics=metrics,
                options={"num_ctx": 4096, "temperature": 0, "num_predict": 600},
                think=False,
            )
            model_selected = parse_skill_names(response.message.content)
        except Exception as exc:
            print(f"[Skill router warning: {type(exc).__name__}: {exc}]")

    selected = []
    for name in model_selected + heuristic_selected:
        if (
            name in SKILLS
            and name not in BASELINE_SKILLS
            and SKILLS.get(name, {}).get("role") != "control"
            and name not in selected
        ):
            selected.append(name)
    return selected[:6]


def loaded_skill_prompt(skill_names):
    parts = []
    for name in skill_names:
        if name not in SKILLS:
            continue
        parts.append(f"\n=== SKILL: {name} ===\n" + load_skill_instructions(name))
    return "\n".join(parts)


def build_system_prompt(active_skills):
    available = "\n".join(
        f"- {name}: {skill.get('description', '')}"
        for name, skill in SKILLS.items()
        if name not in active_skills and skill.get("role") != "control"
    )
    return (
        BASE_SYSTEM_PROMPT
        + "\n\nLOADED SKILLS:\n"
        + loaded_skill_prompt(active_skills)
        + "\n\nOTHER SKILLS AVAILABLE VIA load_skill:\n"
        + available
    )
