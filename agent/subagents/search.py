"""
agent/subagents/search.py — the `subagent_search` worker (web research).

Bounded granite loop whitelisted to the web tools only (web_search, fetch_page).
"""
from tools.web import _web_search, _fetch_webpage
from .core import run_subagent_loop

AGENT_NAME = "subagent_search"

MAX_ROUNDS = 4
MAX_TOOL_CALLS = 3

SYSTEM_PROMPT = (
    "You are a focused web-research worker. You are given ONE specific question. "
    "Use web_search to find the missing facts, and fetch_page to read a specific "
    "URL when a snippet is not enough. When you have the facts, STOP calling tools "
    "and write a short, concrete digest that directly answers the question — "
    "include the key facts, numbers, and any source URLs. Do not pad it. "
    "Stay on the single question you were given; do not branch into related topics."
)

# Ollama tool schemas advertised to granite. Names are deliberately simple.
TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for a focused query. Returns titled result snippets with URLs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The focused search query."}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_page",
            "description": "Fetch and extract the readable text of a specific webpage URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The full URL to fetch."}
                },
                "required": ["url"],
            },
        },
    },
]


def _text(content_list) -> str:
    """Pull the plain text out of a tool's list[TextContent] return value."""
    if not content_list:
        return ""
    return "\n".join(getattr(c, "text", "") for c in content_list).strip()


async def _run_web_search(args: dict) -> str:
    query = (args.get("query") or args.get("target") or "").strip()
    if not query:
        return "web_search needs a 'query'."
    return _text(await _web_search(query))


async def _run_fetch_page(args: dict) -> str:
    url = (args.get("url") or args.get("target") or "").strip()
    if not url:
        return "fetch_page needs a 'url'."
    result = _text(await _fetch_webpage(url))
    # Strip the UI-only link-card metadata line; granite only needs the text.
    lines = [ln for ln in result.split("\n") if not ln.startswith("__LINK_METADATA__:")]
    return "\n".join(lines).strip()


TOOL_RUNNERS = {
    "web_search": _run_web_search,
    "fetch_page": _run_fetch_page,
}


async def run(task: str, *, emit=None) -> dict:
    """Run the web-research subagent for `task`. Returns the core loop result
    dict (digest / transcript / ok)."""
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
