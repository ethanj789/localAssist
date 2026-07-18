"""
plans.py — In-memory plan tracking keyed by conversation_id.
One active plan per conversation. Plans are ephemeral (not persisted to DB).
"""

import logging
from typing import Optional

log = logging.getLogger(__name__)

# { conversation_id: { "goal": str, "steps": [{"description": str, "status": "pending"|"done"|"failed"}], "current_step": int } }
_plans: dict[str, dict] = {}


def create_plan(conversation_id: str, goal: str, steps: list[str]) -> dict:
    """Create (or replace) the active plan for a conversation."""
    plan = {
        "goal": goal,
        "steps": [{"description": s, "status": "pending"} for s in steps],
        "current_step": 0,
    }
    _plans[conversation_id] = plan
    log.info("plan created | conv=%s | goal=%s | steps=%d", conversation_id, goal, len(steps))
    return plan


def update_plan(conversation_id: str, step_index: int | None = None, status: str | None = None, new_steps: list[str] | None = None) -> dict | None:
    """
    Update the active plan for a conversation.
    - step_index + status: mark a specific step as "done" or "failed"
    - new_steps: append additional steps to the plan
    Returns the updated plan, or None if no plan exists.
    """
    plan = _plans.get(conversation_id)
    if not plan:
        return None

    if step_index is not None and status:
        if 0 <= step_index < len(plan["steps"]):
            plan["steps"][step_index]["status"] = status
            # Auto-advance current_step to next pending
            if status == "done" and step_index == plan["current_step"]:
                _advance_current_step(plan)

    if new_steps:
        for s in new_steps:
            plan["steps"].append({"description": s, "status": "pending"})

    return plan


def get_plan(conversation_id: str) -> Optional[dict]:
    """Get the active plan for a conversation, or None."""
    return _plans.get(conversation_id)


def clear_plan(conversation_id: str) -> None:
    """Remove the plan for a conversation."""
    _plans.pop(conversation_id, None)


def format_plan_status(plan: dict) -> str:
    """Format plan state as a compact string to append to tool results."""
    lines = [f"[Plan: {plan['goal']}]"]
    for i, step in enumerate(plan["steps"]):
        status = step["status"]
        if status == "done":
            prefix = "✓"
        elif i == plan["current_step"] and status == "pending":
            prefix = "→"
        elif status == "failed":
            prefix = "✗"
        else:
            prefix = "○"
        lines.append(f"  {prefix} Step {i + 1}: {step['description']}")
    return "\n".join(lines)


def _advance_current_step(plan: dict) -> None:
    """Move current_step to the next pending step."""
    for i in range(len(plan["steps"])):
        if plan["steps"][i]["status"] == "pending":
            plan["current_step"] = i
            return
    # All steps done — point past the end
    plan["current_step"] = len(plan["steps"])
