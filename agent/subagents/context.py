"""
agent/subagents/context.py — the `subagent_context` worker (workspace gathering).

Bounded granite loop whitelisted to read-only workspace tools (search_workspace,
read_file, read_skeleton). Unproven relative to subagent_search — granite driving
a read loop — so the budget is the main guardrail.
"""
from tools.files import _read_file, _read_code_skeleton, _list_files, _is_glob_pattern, _list_files_by_glob
from tools.search import _search_semantic
from .core import run_subagent_loop

AGENT_NAME = "subagent_context"

MAX_ROUNDS = 4
MAX_TOOL_CALLS = 4

SYSTEM_PROMPT = (
    "You are a focused workspace-research worker. You are given ONE specific task "
    "about files in the local workspace. Use search_workspace to find relevant "
    "files, then read_file (or read_skeleton for a structural overview of code) to "
    "read the parts you need. When you have the information, STOP calling tools and "
    "write a short, concrete digest that directly answers the task — name the files "
    "and quote or summarize the relevant content. Do not pad it. Stay on the single "
    "task you were given; do not wander into unrelated files."
)

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "search_workspace",
            "description": (
                "Find files in the workspace. Pass a keyword/phrase to search names and "
                "content, a glob like '*.py' to filter by type, or '*' to list everything."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Keyword, phrase, glob, or '*'."}
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a workspace file by its exact name/path (e.g. 'draft.txt', 'utils/helpers.py').",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Exact file name/path relative to the workspace root."},
                    "start": {"type": "integer", "description": "1-based start line (optional)."},
                    "count": {"type": "integer", "description": "Number of lines to read (optional)."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_skeleton",
            "description": "Get a structural outline (classes/functions) of a code file instead of its full text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Exact code file name/path relative to the workspace root."}
                },
                "required": ["path"],
            },
        },
    },
]


def _text(content_list) -> str:
    """Pull the plain text out of a tool's list[TextContent] return value."""
    if not content_list:
        return ""
    return "\n".join(getattr(c, "text", "") for c in content_list).strip()


async def _run_search_workspace(args: dict) -> str:
    query = (args.get("query") or args.get("topic") or "").strip()
    if not query or query == "*":
        return _text(await _list_files(None))
    if _is_glob_pattern(query):
        return _text(_list_files_by_glob(query))
    # Keyword listing + semantic matches, mirroring the main registry's
    # search_workspace composition but kept lean for the worker.
    keyword = _text(await _list_files(query))
    semantic = _text(await _search_semantic(query, 3))
    parts = []
    if keyword:
        parts.append("=== Keyword Matches ===\n" + keyword)
    if semantic:
        parts.append("=== Semantic Matches ===\n" + semantic)
    return "\n\n".join(parts) if parts else f"No matches found for '{query}'."


async def _run_read_file(args: dict) -> str:
    path = (args.get("path") or "").strip()
    if not path:
        return "read_file needs a 'path'."
    start = args.get("start", 1) or 1
    count = args.get("count")
    try:
        start = int(start)
    except (TypeError, ValueError):
        start = 1
    try:
        count = int(count) if count is not None else None
    except (TypeError, ValueError):
        count = None
    return _text(await _read_file(path, start, count))


async def _run_read_skeleton(args: dict) -> str:
    path = (args.get("path") or "").strip()
    if not path:
        return "read_skeleton needs a 'path'."
    return _text(await _read_code_skeleton(path))


TOOL_RUNNERS = {
    "search_workspace": _run_search_workspace,
    "read_file": _run_read_file,
    "read_skeleton": _run_read_skeleton,
}


async def run(task: str, *, emit=None) -> dict:
    """Run the workspace-research subagent for `task`. Returns the core loop
    result dict (digest / transcript / ok)."""
    return await run_subagent_loop(
        agent_name=AGENT_NAME,
        task=task,
        system_prompt=SYSTEM_PROMPT,
        tools_schema=TOOLS_SCHEMA,
        tool_runners=TOOL_RUNNERS,
        max_rounds=MAX_ROUNDS,
        max_tool_calls=MAX_TOOL_CALLS,
        emit=emit,
    )
