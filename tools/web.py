"""
tools/web.py — Web search and page fetch tool implementations.
Providers: Tavily (primary), DuckDuckGo (available as alternative).
"""
import sys
import json
import logging
import os

import requests
from ddgs import DDGS
from tavily import TavilyClient
from mcp import types

log = logging.getLogger(__name__)

DDG_MAX_RESULTS = 3
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

async def _web_search(query: str) -> list[types.TextContent]:
    results = _tavily_search(query)

    if not results:
        return [types.TextContent(type="text", text="No results found.")]

    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}\n   URL: {r['url']}\n   {r['snippet']}\n")

    return [types.TextContent(type="text", text="\n".join(lines))]


def _tavily_search(query: str) -> list[dict]:
    TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
    try:
        client = TavilyClient(api_key=TAVILY_API_KEY)
        response = client.search(query, max_results=DDG_MAX_RESULTS)
        return [
            {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")}
            for r in response.get("results", [])
        ]
    except Exception as e:
        log.error("Tavily error: %s", e)
        return []


def _ddg_search(query: str) -> list[dict]:
    """DuckDuckGo search (available as an alternative to Tavily)."""
    try:
        with DDGS(timeout=5) as ddgs:
            raw = ddgs.text(query, max_results=DDG_MAX_RESULTS)
            return [
                {"title": r.get("title", ""), "url": r.get("href", ""), "snippet": r.get("body", "")}
                for r in (raw or [])
            ]
    except Exception as e:
        print(f"[DDG error] {e}", file=sys.stderr)
        return []


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
