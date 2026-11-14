"""
agent/subagents/search.py — the `subagent_search` worker (web research).

Bounded granite loop whitelisted to the web tools only (web_search, fetch_page).
"""
from tools.web import _web_search, _fetch_webpage
from tools.events import _recent_events
from .core import run_subagent_loop

AGENT_NAME = "subagent_search"

MAX_ROUNDS = 4
MAX_TOOL_CALLS = 3

SYSTEM_PROMPT = (
    "You are a focused web-research worker. You are given ONE specific question. "
    "FIRST decide the tool: if the question mentions today, now, latest, current, "
    "recent, this week, or any ongoing/breaking event, you MUST use news_search — "
    "NOT web_search. Use web_search only for timeless general facts with no recency. "
    "Use fetch_page to read a specific URL when a snippet is not enough. "
    "When you have the facts, STOP calling tools "
    "and write a short, concrete digest that directly answers the question — "
    "include the key facts, numbers, and any source URLs. Do not pad it. "
    "Stay on the single question you were given; do not branch into related topics."
)

# Ollama tool schemas advertised to granite. Names are deliberately simple.
TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "news_search",
            "description": "Search recent NEWS for a focused query. Use this (not web_search) when the question is about current events, breaking news, or anything time-sensitive — it filters to the last week and prefers news sources.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The focused news query."}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for a focused query. Returns titled result snippets with URLs. Use only for timeless facts, not current events.",
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


async def _run_news_search(args: dict) -> str:
    query = (args.get("query") or args.get("target") or "").strip()
    if not query:
        return "news_search needs a 'query'."
    # Same path as the main agent: GNews free budget first, Tavily-news fallback
    # on empty — keeps news handling uniform across the app.
    return _text(await _recent_events("news", query))


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
    "news_search": _run_news_search,
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
