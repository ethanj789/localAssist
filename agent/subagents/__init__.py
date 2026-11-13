"""
agent/subagents/ — Granite worker subagents.

Router (`run_subagent`) over per-type modules:
    subagent_search  -> search.py   (web research; proven)
    subagent_context -> context.py  (workspace gathering; unproven)

A package rather than one module to prepare for grouping the in-process
"streaming tools" (today plan; later + subagents) intercepted in agent/loop.py.
"""
import logging

from . import search as _search
from . import context as _context

log = logging.getLogger(__name__)

_ROUTER = {
    _search.AGENT_NAME: _search.run,
    _context.AGENT_NAME: _context.run,
}

SUBAGENT_NAMES = frozenset(_ROUTER)


async def run_subagent(name: str, task: str, *, emit=None) -> dict:
    """Dispatch to the subagent identified by `name` and run it on `task`.

    Returns a dict: {"digest": str (never empty), "transcript": list, "ok": bool}.
    An unknown name returns a failure-shaped result rather than raising, so the
    caller can always append a valid (non-empty) tool result to history.
    """
    runner = _ROUTER.get(name)
    if runner is None:
        log.warning("run_subagent: unknown subagent %r", name)
        return {
            "digest": (
                f"Subagent '{name}' does not exist. Available: "
                f"{', '.join(sorted(SUBAGENT_NAMES))}."
            ),
            "transcript": [],
            "ok": False,
        }
    task = (task or "").strip()
    if not task:
        return {
            "digest": f"Subagent {name} was called with an empty task. Provide a specific query.",
            "transcript": [],
            "ok": False,
        }
    return await runner(task, emit=emit)
