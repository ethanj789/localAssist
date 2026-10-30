"""The agent package — formerly the monolithic agent.py.

This __init__ is a thin facade that re-exports the public surface the rest of
the project depends on, so existing imports keep working unchanged:

    import agent
    from agent import agent_loop
    agent.build_system_prompt(), agent.build_ollama_tools(...)
    agent._resolve_preview(...), agent.resume_after_confirmation(...)

Implementation lives in focused submodules:
    sse          — _sse SSE line formatter
    prompt       — build_system_prompt, build_ollama_tools
    attachments  — file/page-image + extracted-text rebuild (KV-cache critical)
    history      — prune / summarize helpers (remote-only; skipped on Ollama)
    models       — per-turn model selection + background title generation
    parsing      — text→tool_call fallback parser for small local models
    edits        — _resolve_preview + the frozen edit-result contract strings
    loop         — _run_tool_loop, the shared provider/tool engine
    turn         — agent_loop, resume_after_confirmation (the two entrypoints)

`REQUIRES_CONFIRMATION` (the confirm-required tool set) lives in `config`, a
leaf module, because `db.get_pending_tool_call` also needs it and `db` must not
import `agent` (that would form a db<->agent import cycle). The edit-result
contract strings and `AUTO_REJECT_EXTRA_EDIT` live in `agent/edits.py` — they
are used only inside the agent package. Both are re-exported here under their
original names for backward compatibility.
"""

from .sse import _sse
from .prompt import build_system_prompt, build_ollama_tools
from .attachments import (
    _file_page_b64s,
    _file_text_block,
    _apply_file_context,
    _rehydrate_history_files,
)
from .history import (
    _prune_history,
    _maybe_summarize_history,
    _summarize_tool_result,
)
from .models import generate_and_save_title, _resolve_turn_models
from .parsing import _extract_tool_call_from_text
from .edits import (
    _resolve_preview,
    REQUIRES_CONFIRMATION,
    AUTO_REJECT_EXTRA_EDIT,
    _applied_result,
    _stale_result,
    _invalid_edit_result,
    _read_only_edit_result,
    _reject_result,
)
from .loop import _run_tool_loop
from .turn import agent_loop, resume_after_confirmation

__all__ = [
    # confirm-required policy (from config) + contract strings (from .edits)
    "REQUIRES_CONFIRMATION",
    "AUTO_REJECT_EXTRA_EDIT",
    # prompt / tool assembly
    "build_system_prompt",
    "build_ollama_tools",
    # entrypoints
    "agent_loop",
    "resume_after_confirmation",
    # edit resolution
    "_resolve_preview",
    # SSE helper
    "_sse",
    # internal helpers kept importable for backward compat
    "_file_page_b64s",
    "_file_text_block",
    "_apply_file_context",
    "_rehydrate_history_files",
    "_prune_history",
    "_maybe_summarize_history",
    "_summarize_tool_result",
    "generate_and_save_title",
    "_resolve_turn_models",
    "_extract_tool_call_from_text",
    "_applied_result",
    "_stale_result",
    "_invalid_edit_result",
    "_read_only_edit_result",
    "_reject_result",
    "_run_tool_loop",
]
