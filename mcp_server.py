"""
mcp_server.py — MCP-compliant tool server
Exposes: web_search, fetch_webpage
Transport: stdio (standard MCP pattern)
"""
import os
from pathlib import Path
import sys
import json
import asyncio
import trafilatura
import requests
from duckduckgo_search import DDGS
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types
from tavily import TavilyClient
from dotenv import load_dotenv
import logging
from datetime import datetime

logging.basicConfig(
    filename="logs.txt",
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)
load_dotenv()

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
WORKSPACE_ROOT = (Path(__file__).parent / os.getenv("WORKSPACE", "aiWorkspace")).resolve()
# ── Config (override via env vars if needed) ─────────────────────────────────
DDG_MAX_RESULTS = 3           # how many DDG results to return
FETCH_TIMEOUT   = 8           # seconds before page fetch gives up
MAX_TEXT_CHARS  = 2000        # truncate extracted page text to this length

# WORKSPACE_ROOT = (Path(__file__).parent / "aiWorkspace").resolve()
MAX_FILE_LINES = 500
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "dist", "build"}

app = Server("local-search-mcp")


# ── Tool: web_search ─────────────────────────────────────────────────────────
@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="web_search",
            description=(
                "Search the web using DuckDuckGo (Brave fallback if configured). "
                "Returns a list of results with title, URL, and snippet."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query string."
                    }
                },
                "required": ["query"]
            }
        ),
        types.Tool(
                name="fetch_webpage",
                description=(
                    "Fetch and extract readable text content from a web URL. "
                    "Only use this for http/https URLs from the internet, never for local files."
                    "Note: some major news sites (CNN, WSJ, NYT) block content extraction. "
                    "If a fetch fails, try a different source from the search results."
                ),
                inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The full URL of the page to fetch."
                    }
                },
                "required": ["url"]
            }
        ),
        types.Tool(
            name="list_files",
            description=(
                "List all local files in the workspace. "
                "Use this to see what files the user has made available locally. "
                "Never use fetch_webpage for local files — use read_file instead."
            ),
            inputSchema={
                "type": "object",
                "properties": {}
            }
        ),
        types.Tool(
            name="read_file",
            description=(
                "Read the contents of a file in the workspace. "
                "Pass a relative path like 'server.py' or 'utils/helpers.py'. "
                "Do not include leading slashes or parent directory references."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file, relative to workspace root."}
                },
                "required": ["path"]
            }
        ),
        types.Tool(
            name="calculate",
            description=(
                "Evaluate a mathematical expression and return the result. "
                "Use this for any arithmetic, algebra, or numeric calculation instead of computing mentally. "
                "Pass a valid Python math expression as a string."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "description": "Math expression to evaluate, e.g. '2 ** 32' or '(15 * 8) / 3'"}
                },
                "required": ["expression"]
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    if name == "web_search":
        return await _web_search(arguments["query"])
    elif name == "fetch_webpage":
        return await _fetch_webpage(arguments["url"])
    elif name == "list_files":
        return await _list_files()
    elif name == "read_file":
        return await _read_file(arguments["path"])
    elif name == "calculate":
        return await _calculate(arguments["expression"])

    else:
        raise ValueError(f"Unknown tool: {name}")


# ── Search implementation ─────────────────────────────────────────────────────
async def _web_search(query: str) -> list[types.TextContent]:
    results = _ddg_search(query)
    from_where = "ddg"
    # Tavily fallback if DDG failed
    if not results and TAVILY_API_KEY:
        results = _tavily_search(query)
        from_where = "tav"
    
    log.info("web_search | source=%s | results=%s", from_where, results)

    if not results:
        return [types.TextContent(type="text", text="No results found.")]

    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}\n   URL: {r['url']}\n   {r['snippet']}\n")

    return [types.TextContent(type="text", text="\n".join(lines))]


def _tavily_search(query: str) -> list[dict]:
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

# ── Page fetch implementation ─────────────────────────────────────────────────
# async def _fetch_webpage(url: str) -> list[types.TextContent]:
#     log.info("fetch_webpage | url=%s", url)
#     try:
#         downloaded = trafilatura.fetch_url(url)
#         if not downloaded:
#             return [types.TextContent(type="text", text=f"Could not fetch: {url}")]

#         text = trafilatura.extract(
#             downloaded,
#             include_comments=False,
#             include_tables=True,
#             no_fallback=False
#         )

#         if not text:
#             return [types.TextContent(type="text", text=f"No readable content extracted from: {url}")]

#         if len(text) > MAX_TEXT_CHARS:
#             text = text[:MAX_TEXT_CHARS] + f"\n\n[... truncated at {MAX_TEXT_CHARS} chars]"

#         return [types.TextContent(type="text", text=f"Content from {url}:\n\n{text}")]

#     except Exception as e:
#         log.error("fetch_webpage error | url=%s | %s", url, e)
#         return [types.TextContent(type="text", text=f"Error fetching page: {e}")]

#switch to tavily's api
async def _fetch_webpage(url: str) -> list[types.TextContent]:
    try:
        client = TavilyClient(api_key=TAVILY_API_KEY)

        resp = client.extract(urls=[url])

        results = resp.get("results", [])
        if not results:
            return [types.TextContent(type="text", text="No content found.")]

        r = results[0]

        title = r.get("title", "")
        content = r.get("content", "")
        failed = resp.get("failed_results", [])
        if failed:
            log.warning("fetch_webpage | failed_results=%s", failed)
        # truncate for small models
        MAX_CHARS = 2000
        if len(content) > MAX_CHARS:
            content = content[:MAX_CHARS] + "\n...[truncated]"

        text = f"Title: {title}\nURL: {url}\n\nContent:\n{content}"        
        return [types.TextContent(type="text", text=text)]

    except Exception as e:
        log.error("fetch_webpage error: %s", e)
        return [types.TextContent(type="text", text="Failed to fetch webpage.")]

async def _list_files() -> list[types.TextContent]:
    log.info("list_files | listing workspace root")
    target = WORKSPACE_ROOT
    lines = []
    for root, dirs, files in os.walk(target):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        level = Path(root).relative_to(WORKSPACE_ROOT)
        prefix = str(level) if str(level) != "." else ""
        lines.append(f"{prefix}/" if prefix else "/")
        for f in files:
            lines.append(f"  {f if not prefix else prefix + '/' + f}")
    return [types.TextContent(type="text", text="\n".join(lines) or "No files found.")]

# ── File tools ────────────────────────────────────────────────────────────────
async def _read_file(path: str) -> list[types.TextContent]:
    log.info("read_file | path=%s", path)
    target = (WORKSPACE_ROOT / path).resolve()
    try:
        target.relative_to(WORKSPACE_ROOT)
    except ValueError:
        return [types.TextContent(type="text", text="Access denied.")]

    if target.is_symlink():
        return [types.TextContent(type="text", text="Access denied: symlinks not allowed.")]

    try:
        target.resolve(strict=True)
    except FileNotFoundError:
        return [types.TextContent(type="text", text=f"File not found: {path}")]

    lines = target.read_text(errors="replace").splitlines()
    truncated = len(lines) > MAX_FILE_LINES
    content = "\n".join(lines[:MAX_FILE_LINES])
    if truncated:
        content += f"\n\n[... truncated at {MAX_FILE_LINES} lines]"

    return [types.TextContent(type="text", text=f"Contents of {path}:\n\n{content}")]
#calculate 
async def _calculate(expression: str) -> list[types.TextContent]:
    log.info("calculate | expr=%s", expression)
    try:
        # restrict to safe math only — no builtins, no imports
        allowed_names = {k: v for k, v in vars(__import__("math")).items() if not k.startswith("_")}
        result = eval(expression, {"__builtins__": {}}, allowed_names)
        return [types.TextContent(type="text", text=f"{expression} = {result}")]
    except Exception as e:
        return [types.TextContent(type="text", text=f"Could not evaluate '{expression}': {e}")]


# ── Entry point ───────────────────────────────────────────────────────────────
async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())
