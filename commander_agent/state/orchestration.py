from __future__ import annotations

from commander_agent.core.bounded_state import short, compact_control
from copy import deepcopy
from pathlib import Path
import json

from commander_agent.state.evidence import current_evidence_version
from commander_agent.state.io import stable_hash

_HIERARCHY_FILE = Path(__file__).resolve().parents[1] / "skills" / "hierarchy.json"


def _load_hierarchy():
    try:
        data = json.loads(_HIERARCHY_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


_HIERARCHY = _load_hierarchy()
STAGES = [x.get("id") for x in _HIERARCHY.get("stages", []) if isinstance(x, dict) and x.get("id")]
RECOVERY_CAPABILITIES = [
    x.get("name") for x in _HIERARCHY.get("recovery_capabilities", [])
    if isinstance(x, dict) and x.get("name")
]
SAME_SCOPE_CAPABILITIES = [
    x.get("name") for x in _HIERARCHY.get("recovery_capabilities", [])
    if isinstance(x, dict) and x.get("name") and x.get("scope_class") == "same_scope"
]
TERMINAL_CAPABILITIES = list(_HIERARCHY.get("terminal_capabilities", []))
ALL_CAPABILITIES = RECOVERY_CAPABILITIES + TERMINAL_CAPABILITIES


def _normalise_gap(text):
    return " ".join(str(text or "").strip().lower().split())


def _goal_signature(text):
    return stable_hash({"goal": _normalise_gap(text)})


def ensure_orchestration_state(case_state, initial_goal="Resolve the current evidence gap without violating question scope."):
    state = case_state.setdefault(
        "orchestration",
        {
            "stage": "RECOVERY",
            "current_goal": initial_goal,
            "remaining_gap": initial_goal,
            "goal_revision": 1,
            "goal_signature": _goal_signature(initial_goal),
            "attempts": [],
            "exhausted": [],
            "irrelevant": [],
            "irrelevant_by_version": {},
            "irrelevant_by_goal": {},
            "blocked": [],
            "supervisor_history": [],
            "recommended": [],
            "last_progress_version": current_evidence_version(case_state),
        },
    )
    # Upgrade older saved/in-memory states without discarding investigation history.
    state.setdefault("goal_revision", 1)
    state.setdefault("goal_signature", _goal_signature(state.get("remaining_gap") or initial_goal))
    state.setdefault("irrelevant_by_goal", {})
    state.setdefault("irrelevant_by_version", {})
    state.setdefault("attempts", [])
    state.setdefault("recommended", [])
    return state


def _uniq(values):
    out = []
    for value in values or []:
        if value and value not in out:
            out.append(value)
    return out


def set_stage(case_state, stage, goal=None, remaining_gap=None):
    state = ensure_orchestration_state(case_state)
    if stage in STAGES:
        state["stage"] = stage
    new_gap = remaining_gap if remaining_gap is not None else goal
    if goal:
        state["current_goal"] = short(goal)
    if remaining_gap is not None:
        state["remaining_gap"] = short(remaining_gap)
    if new_gap:
        sig = _goal_signature(new_gap)
        if sig != state.get("goal_signature"):
            state["goal_revision"] = int(state.get("goal_revision", 1)) + 1
            state["goal_signature"] = sig
    return state


def record_capability_attempt(
    case_state,
    capability,
    outcome,
    evidence_before=None,
    evidence_after=None,
    note="",
    action_signature="",
    prerequisites=None,
):
    state = ensure_orchestration_state(case_state)
    if evidence_before is None:
        evidence_before = current_evidence_version(case_state)
    if evidence_after is None:
        evidence_after = current_evidence_version(case_state)
    item = {
        "capability": capability,
        "outcome": outcome,
        "prerequisites": list(prerequisites or []),
        "dependency_snapshot": dependency_snapshot(case_state, prerequisites or []),
        "evidence_before": int(evidence_before),
        "evidence_after": int(evidence_after),
        "goal_revision": int(state.get("goal_revision", 1)),
        "goal_signature": str(state.get("goal_signature") or ""),
        "note": short(note),
        "action_signature": str(action_signature or ""),
    }
    state["attempts"].append(item)
    if int(evidence_after) > int(evidence_before):
        state["last_progress_version"] = int(evidence_after)
    if outcome in {
        "failed", "blocked", "irrelevant", "no_new_evidence", "insufficient",
        "contract_failure", "execution_failure",
    }:
        key = "blocked" if outcome == "blocked" else ("irrelevant" if outcome == "irrelevant" else "exhausted")
        state[key] = _uniq(state.get(key, []) + [capability])
    return item


def mark_capabilities(case_state, *, exhausted=None, irrelevant=None, blocked=None):
    state = ensure_orchestration_state(case_state)
    state["exhausted"] = _uniq(state.get("exhausted", []) + list(exhausted or []))
    state["irrelevant"] = _uniq(state.get("irrelevant", []) + list(irrelevant or []))
    state["blocked"] = _uniq(state.get("blocked", []) + list(blocked or []))
    if irrelevant:
        version = str(current_evidence_version(case_state))
        by_version = state.setdefault("irrelevant_by_version", {})
        by_version[version] = _uniq(by_version.get(version, []) + list(irrelevant or []))
        goal = str(state.get("goal_signature") or "")
        by_goal = state.setdefault("irrelevant_by_goal", {})
        by_goal[goal] = _uniq(by_goal.get(goal, []) + list(irrelevant or []))
    return state


def dependency_snapshot(case_state, prerequisites):
    attempts = ensure_orchestration_state(case_state).get("attempts", [])
    return {cap: sum(1 for a in attempts if a.get("capability") == cap and a.get("outcome") in {"progress", "analytical_progress"}) for cap in prerequisites}


def dependency_blocked(case_state, capability):
    attempts = [a for a in ensure_orchestration_state(case_state)["attempts"] if a.get("capability") == capability]
    if not attempts or attempts[-1].get("outcome") != "missing_prerequisite":
        return False
    last = attempts[-1]
    current = dependency_snapshot(case_state, last.get("prerequisites", []))
    return not current or any(current[k] <= last.get("dependency_snapshot", {}).get(k, 0) for k in current)


def attempted_at_current_version(case_state, capability):
    state = ensure_orchestration_state(case_state)
    version = current_evidence_version(case_state)
    return any(
        item.get("capability") == capability
        and item.get("outcome") != "missing_prerequisite"
        and int(item.get("evidence_before", -1)) == int(version)
        and int(item.get("evidence_after", -1)) == int(version)
        for item in state.get("attempts", [])
    )


def attempted_for_current_goal(case_state, capability):
    """Prevent skill loops even when another action incremented evidence version.

    A capability may become useful again only after the Supervisor changes the unresolved
    goal/gap. This is stronger than evidence-version-only loop prevention while remaining
    dynamic: a genuinely new gap creates a new goal signature and re-opens relevant skills.
    """
    state = ensure_orchestration_state(case_state)
    goal = str(state.get("goal_signature") or "")
    return any(
        item.get("capability") == capability
        and str(item.get("goal_signature") or "") == goal
        and item.get("outcome") in {
            "progress", "failed", "blocked", "no_new_evidence", "insufficient",
            "contract_failure", "execution_failure", "analytical_progress",
        }
        for item in state.get("attempts", [])
    )


def available_recovery_capabilities(case_state, supervisor_recommended=None):
    """Return useful recovery capabilities for the *current unresolved goal*.

    Same-scope capabilities always precede scope expansion. A capability already attempted
    for the same unresolved goal is not repeated, even if unrelated evidence increments the
    evidence version. If the Supervisor identifies a genuinely different gap, the goal
    signature changes and previously used capabilities may become eligible again.
    """
    state = ensure_orchestration_state(case_state)
    version = str(current_evidence_version(case_state))
    goal = str(state.get("goal_signature") or "")
    irrelevant_now = set((state.get("irrelevant_by_version") or {}).get(version, []))
    irrelevant_now |= set((state.get("irrelevant_by_goal") or {}).get(goal, []))

    same_scope = [
        cap for cap in SAME_SCOPE_CAPABILITIES
        if cap not in irrelevant_now
        and not any(a.get("capability") == cap and a.get("outcome") == "irrelevant" and a.get("goal_signature") == goal for a in state["attempts"])
        and not dependency_blocked(case_state, cap)
        and not attempted_for_current_goal(case_state, cap)
        and not attempted_at_current_version(case_state, cap)
    ]
    scope = []
    if (
        not same_scope
        and "scope_expansion" not in irrelevant_now
        and not attempted_for_current_goal(case_state, "scope_expansion")
        and not attempted_at_current_version(case_state, "scope_expansion")
    ):
        scope = ["scope_expansion"]

    available = same_scope + scope
    recommended = [
        x for x in (supervisor_recommended or state.get("recommended") or [])
        if x in available
    ]
    if recommended:
        available = recommended + [x for x in available if x not in recommended]
    return available


def can_stop_unresolved(case_state):
    return len(available_recovery_capabilities(case_state)) == 0


def record_supervisor_evaluation(case_state, evaluation):
    state = ensure_orchestration_state(case_state)
    item = compact_control(deepcopy(evaluation or {}))
    item["evidence_version"] = current_evidence_version(case_state)

    if item.get("canonical_release_ready"):
        state["remaining_gap"] = ""
        state["current_goal"] = "Validate canonical deterministic candidate."
        state["recommended"] = []
        new_gap = ""
    else:
        new_gap = str(item.get("remaining_gap") or state.get("remaining_gap") or "").strip()
    if new_gap:
        new_sig = _goal_signature(new_gap)
        if item.get("invalidate_from_stage") in {"UNDERSTAND", "CLASSIFY", "PRIMARY_ANALYSIS"} and new_sig != state.get("goal_signature"):
            state["goal_revision"] = int(state.get("goal_revision", 1)) + 1
            state["goal_signature"] = new_sig
        state["remaining_gap"] = new_gap
        state["current_goal"] = "Resolve: " + new_gap

    state["supervisor_history"].append(item)
    state["recommended"] = _uniq(item.get("next_capabilities") or [])
    mark_capabilities(case_state, irrelevant=item.get("irrelevant_capabilities") or [])

    stage = item.get("invalidate_from_stage")
    if stage in STAGES:
        state["stage"] = stage
    elif item.get("allow_validation"):
        state["stage"] = "VALIDATE"
    else:
        state["stage"] = "RECOVERY"
    return item


def action_signature(case_state, capability, payload):
    state = ensure_orchestration_state(case_state)
    return stable_hash(
        {
            "evidence_version": current_evidence_version(case_state),
            "goal_signature": state.get("goal_signature"),
            "capability": capability,
            "payload": payload,
        }
    )


def supervisor_context(case_state):
    state = ensure_orchestration_state(case_state)
    return {
        "stage": state.get("stage"),
        "current_goal": state.get("current_goal"),
        "remaining_gap": state.get("remaining_gap"),
        "goal_revision": state.get("goal_revision"),
        "goal_signature": state.get("goal_signature"),
        "evidence_version": current_evidence_version(case_state),
        "attempts": compact_control(list(state.get("attempts", []))[-6:]),
        "exhausted": list(state.get("exhausted", [])),
        "irrelevant": list(state.get("irrelevant", [])),
        "blocked": list(state.get("blocked", [])),
        "recommended": list(state.get("recommended", [])),
        "available_recovery_capabilities": available_recovery_capabilities(case_state),
    }
