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
import httpx
import ast
import operator as op

logging.basicConfig(
    filename="logs.txt",
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)
load_dotenv()

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
GNEWS_API_KEY = os.getenv("GNEWS_API_KEY", "")
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
        types.Tool(
            name="recent_events",
            description=(
                "Fetch recent information such as news or weather. "
                "Use this when the user asks about current events, latest updates, or weather conditions. "
                "For news, provide a topic or keyword (e.g. 'AI', 'Ukraine'). "
                "For weather, provide a city name (e.g. 'Miami', 'London')."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "infoType": {
                        "type": "string",
                        "description": "Type of information to retrieve. Must be either 'news' or 'weather'."
                    },
                    "details": {
                        "type": "string",
                        "description": "Search query for news or city name for weather."
                    }
                },
                "required": ["infoType", "details"]
            }
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
    elif name == "recent_events":
        return await _recent_events(arguments["type"], arguments["details"]) #type = news/weather, details = location/news topic

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
SAFE_OPS = {
    ast.Add: op.add,
    ast.Sub: op.sub,
    ast.Mult: op.mul,
    ast.Div: op.truediv,
    ast.Pow: op.pow,
    ast.Mod: op.mod,
    ast.USub: op.neg,
    ast.UAdd: op.add
}

async def _calculate(expression: str):
    try:
        tree = ast.parse(expression, mode="eval")
        result = _eval(tree.body)
        return [types.TextContent(type="text", text=f"{expression} = {result}")]
    except Exception as e:
        return [types.TextContent(type="text", text=f"Error: {e}")]

def _eval(node):
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        raise ValueError("Invalid constant")

    if isinstance(node, ast.BinOp):
        left = _eval(node.left)
        right = _eval(node.right)

        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            if right == 0:
                raise ValueError("Division by zero")
            return left / right
        if isinstance(node.op, ast.Mod):
            if right == 0:
                raise ValueError("Modulo by zero")
            return left % right
        if isinstance(node.op, ast.Pow):
            if abs(right) > 1000:
                raise ValueError("Exponent too large")
            result = left ** right
            if abs(result) > 1e100:
                raise ValueError("Result too large")
            return result

        raise ValueError("Unsupported operation")

    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.UAdd):
            return _eval(node.operand)
        if isinstance(node.op, ast.USub):
            return -_eval(node.operand)
        raise ValueError("Unsupported unary operation")

    raise ValueError("Unsupported expression")  
# async def _calculate(expression: str) -> list[types.TextContent]:
#     log.info("calculate | expr=%s", expression)
#     try:
#         # restrict to safe math only — no builtins, no imports
#         allowed_names = {k: v for k, v in vars(__import__("math")).items() if not k.startswith("_")}
#         result = eval(expression, {"__builtins__": {}}, allowed_names)
#         return [types.TextContent(type="text", text=f"{expression} = {result}")]
#     except Exception as e:
#         return [types.TextContent(type="text", text=f"Could not evaluate '{expression}': {e}")]

# recent events getter
async def _recent_events(infoType: str, details: str):
    log.info(f"{infoType}, for {details}")
    try:
        if infoType == "news":
            return _gnews(details)
        elif infoType == "weather":
            # return _weather(details)
            return _weather_simple(details)

    except Exception as e:
        return [types.TextContent(type="text", text=f"failed getting info on {infoType}': {e}")]

async def _weather_simple(city: str) -> str:
    try:
        url = f"https://wttr.in{city}"
        params = {"format": "j1"}
        headers = {"User-Agent": "curl/7.68.0"}
        
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, params=params, headers=headers)
            if r.status_code != 200:
                return "Weather fetch failed."
            data = r.json()

        # 1. Current Weather
        curr = data["current_condition"][0]
        
        # 2. Get local time to find future slots (0-23)
        # localObsDateTime looks like "2023-10-27 10:15 AM"
        import datetime
        obs_time = datetime.datetime.strptime(curr['localObsDateTime'], "%Y-%m-%d %I:%M %p")
        curr_hour = obs_time.hour

        def get_hourly_data(target_hour):
            # Slots are every 3 hours: 0, 3, 6, 9, 12, 15, 18, 21
            day_offset = target_hour // 24
            hour_in_day = target_hour % 24
            slot_idx = min(hour_in_day // 3, 7) # Round down to nearest 3-hour slot
            
            day_data = data["weather"][day_offset]
            hour_data = day_data["hourly"][slot_idx]
            return f"{hour_data['tempF']}°F & {hour_data['weatherDesc'][0]['value']}"
        return (
            f"Weather for {city}:\n"
            f"NOW: {curr['temp_F']}°F, {curr['weatherDesc'][0]['value']}\n"
            f"+6H:  {get_hourly_data(curr_hour + 6)}\n"
            f"+12H: {get_hourly_data(curr_hour + 12)}\n"
            f"+18H: {get_hourly_data(curr_hour + 18)}\n"
            f"+24H: {get_hourly_data(curr_hour + 24)}"
        )
    except Exception as e:
        return f"Error: {e}"
        
async def _weather(lat: float, lon: float):
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "current_weather": True
    }

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, params=params)
        return r.json()


async def _gnews(query: str):
    url = "https://gnews.io/api/v4/search"
    params = {
        "q": query,
        "lang": "en",
        "max": 5,
        "token": GNEWS_API_KEY
    }

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, params=params)
        data = r.json()    

    return [
        {
            "title": a["title"],
            "url": a["url"],
            "snippet": a["description"]
        }
        for a in data.get("articles", [])
    ]


# ── Entry point ───────────────────────────────────────────────────────────────
async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())
