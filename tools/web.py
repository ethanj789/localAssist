"""
tools/web.py — Web search and page fetch tool implementations.
Providers: Tavily (primary), DuckDuckGo (available as alternative).
"""
import sys
import json
import logging
import os

import requests
# from ddgs import DDGS
from tavily import TavilyClient
from mcp import types

log = logging.getLogger(__name__)

MAX_SEARCH_RESULTS = 10
FETCH_TIMEOUT   = 8
MAX_TEXT_CHARS  = 2000


# ── web_search ────────────────────────────────────────────────────────────────

# Alternative implementation (DDG primary, Tavily fallback):
# async def _web_search(query: str) -> list[types.TextContent]:
#     results = _ddg_search(query)
#     from_where = "ddg"
#     if not results and TAVILY_API_KEY:
#         results = _tavily_search(query)
#         from_where = "tav"
#
#     log.info("web_search | source=%s | results=%s", from_where, results)
#     if not results:
#         return [types.TextContent(type="text", text="No results found.")]
#
#     lines = []
#     for i, r in enumerate(results, 1):
#         lines.append(f"{i}. {r['title']}\n   URL: {r['url']}\n   {r['snippet']}\n")
#     return [types.TextContent(type="text", text="\n".join(lines))]

async def _web_search(query: str, topic: str = "general", time_range: str | None = None) -> list[types.TextContent]:
    answer, results = _tavily_search(query, topic=topic, time_range=time_range)

    if not results:
        return [types.TextContent(type="text", text="No results found.")]

    lines = []
    if answer:
        lines.append(f"Summary: {answer}\n")
    for i, r in enumerate(results, 1):
        date = f" ({r['published_date']})" if r.get("published_date") else ""
        lines.append(f"{i}. {r['title']}{date}\n   URL: {r['url']}\n   {r['snippet']}\n")

    return [types.TextContent(type="text", text="\n".join(lines))]


def _tavily_search(query: str, topic: str = "general", time_range: str | None = None) -> tuple[str, list[dict]]:
    """Returns (answer, results). `answer` is Tavily's own LLM synthesis of the
    results (include_answer) — free on a basic search — so our small local model
    gets a pre-digested summary instead of having to distill raw snippets itself.
    Empty string if Tavily returned none."""
    TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
    # Not setting filter_by_published_date on purpose — it also drops sources with
    # no detectable date, which can lose a fresh article Tavily can't date-parse.
    kwargs: dict = {"max_results": MAX_SEARCH_RESULTS, "topic": topic, "include_answer": True}
    if topic == "news":
        kwargs["include_published_date"] = True
    if time_range is not None:
        kwargs["time_range"] = time_range
    try:
        client = TavilyClient(api_key=TAVILY_API_KEY)
        response = client.search(query, **kwargs)
        results = [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("content", ""),
                "published_date": r.get("published_date", ""),
            }
            for r in response.get("results", [])
        ]
        return response.get("answer") or "", results
    except Exception as e:
        log.error("Tavily error: %s", e)
        return "", []


# def _ddg_search(query: str) -> list[dict]:
#     """DuckDuckGo search (available as an alternative to Tavily)."""
#     try:
#         with DDGS(timeout=5) as ddgs:
#             raw = ddgs.text(query, max_results=MAX_SEARCH_RESULTS)
#             return [
#                 {"title": r.get("title", ""), "url": r.get("href", ""), "snippet": r.get("body", "")}
#                 for r in (raw or [])
#             ]
#     except Exception as e:
#         print(f"[DDG error] {e}", file=sys.stderr)
#         return []


# ── fetch_webpage ─────────────────────────────────────────────────────────────

async def _fetch_webpage(url: str) -> list[types.TextContent]:
    TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
    try:
        client = TavilyClient(api_key=TAVILY_API_KEY)

        resp = client.extract(urls=[url])

        results = resp.get("results", [])
        if not results:
            return [types.TextContent(type="text", text="No content found.")]

        r = results[0]

        title = r.get("title", "")
        url_result = r.get("url", url)
        content = r.get("content", "")
        failed = resp.get("failed_results", [])
        if failed:
            log.warning("fetch_webpage | failed_results=%s", failed)

        # Truncate for small models
        MAX_CHARS = 2000
        if len(content) > MAX_CHARS:
            content = content[:MAX_CHARS] + "\n...[truncated]"

        metadata = {
            "url": url_result,
            "title": title,
            "description": content[:200].strip(),
            "favicon": f"https://www.google.com/s2/favicons?domain={url_result}&sz=32",
            "image": None,
        }

        text = f"Title: {title}\nURL: {url}\n\nContent:\n{content}"
        meta_line = f"\n__LINK_METADATA__:{json.dumps(metadata)}"

        return [types.TextContent(type="text", text=text + meta_line)]

    except Exception as e:
        log.error("fetch_webpage error: %s", e)
        return [types.TextContent(type="text", text="Failed to fetch webpage.")]
