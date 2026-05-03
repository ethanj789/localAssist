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
from ddgs import DDGS
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
import re

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
MAX_FILE_LINES = 250
MAX_SCAN_BYTES = 1_000_000

SKIP_DIRS = {
    "node_modules", ".git", "venv", ".venv", "__pycache__",
    "dist", "build", ".mypy_cache", ".pytest_cache",
}

SCANNABLE_EXTENSIONS = {
    ".py", ".txt", ".md", ".json", ".yaml", ".yml", ".toml",
    ".ts", ".tsx", ".js", ".jsx", ".html", ".css", ".sh",
    ".env.example", ".java",
}

# define raw → normalize → assign once
_RAW_SENSITIVE_FILENAMES = {
    ".env", ".env.local", ".env.production", ".env.development",
    "id_rsa", "id_ed25519", ".netrc", ".htpasswd",
}

_RAW_SENSITIVE_PATTERNS = {
    "key", "secret", "token", "password", "credential",
}

SENSITIVE_FILENAMES = {s.lower() for s in _RAW_SENSITIVE_FILENAMES}
SENSITIVE_PATTERNS = {s.lower() for s in _RAW_SENSITIVE_PATTERNS}

app = Server("local-search-mcp")


# ── Tool: web_search ─────────────────────────────────────────────────────────
@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="web_search",
            description=(
                "Search the web using Tavily. "
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
                "List files in the workspace. "
                "If a `topic` is provided, the tool filters the results to files whose "
                "filenames or contents are relevant to that term, effectively acting as a "
                "search-by-keyword utility. "
                "Use this to discover which files are available before reading any of them. "
                "Never use `fetch_webpage` for local files — use `read_file` instead."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": (
                            "Optional search term to match against file names and file "
                            "content. If omitted, all files are returned."
                        )
                    }
                },
                "required": []
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
                    "path": {
                        "type": "string",
                        "description": "Path to the file, relative to workspace root."
                    },
                    "start": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "1-based line number to start reading from (inclusive). Defaults to 1."
                    },
                    "count": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Number of lines to read starting from 'start'."
                    }
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
            name="read_code_skeleton",
            description=(
                "Read the structural outline of a file (imports, classes, and function signatures). "
                "Use this FIRST on files to understand the architecture and find the exact "
                "functions you need before using `read_file` to read the specific lines."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file, relative to workspace root."
                    }
                },
                "required": ["path"]
            }
        ),
        types.Tool(
            name="recent_events",
            description=(
                "Fetch recent news or weather information. "

                "This tool returns a unified, up-to-date overview of current events "
                "(not raw articles or search results). It is optimized for big-picture summaries. "

                "For NEWS, you may provide either a general category or a specific topic. "
                "Supported categories include: technology, sports, business, health, science, "
                "entertainment, world, nation, general. "

                "Normalize the input before calling this tool: "

                "- If the request includes phrases like 'news', 'top news', 'latest news', "
                "'headlines', 'today's news', or 'current events', map it to 'general'. "

                "- If a supported category word is present, use that exact category. "

                "- Otherwise extract only core keywords (no filler words). "

                "- NEVER pass phrases like 'news about', 'top news', 'latest', or full sentences. "
                "If nothing remains after cleaning, use 'general'. "

                "Examples: "
                "'top news for today' -> 'general', "
                "'latest headlines' -> 'general', "
                "'tech news' -> 'technology', "
                "'Tesla news' -> 'Tesla'. "

                "For WEATHER, provide ONLY a city name (e.g. Miami, London). Do not add things like `tommorow`. "
                "The response will have information for the next twenty four hours"
                "Always prefer this tool over web_search for current events or weather."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "infoType": {
                        "type": "string",
                        "enum": ["news", "weather"],
                        "description": "Type of request: news or weather."
                    },
                    "details": {
                        "type": "string",
                        "description": (
                            "For news: a topic, keyword, entity, or optional category. "
                            "You may use broad categories like technology, sports, business, health, science, "
                            "entertainment, world, nation, general if applicable, but they are NOT required. "
                            "You can also pass specific names (e.g. 'Olivia Rodrigo', 'Tesla', 'AI regulation'), "

                            "For weather: a city name (e.g. Miami, London)."
                        )
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
        topic = arguments.get("topic") or None  # "" and missing both become None
        return await _list_files(topic)
    elif name == "read_file":
        return await _read_file(arguments["path"], arguments.get("start", 1), arguments.get("count", None))
    elif name == "read_code_skeleton":
        return await _read_code_skeleton(arguments["path"])
    elif name == "calculate":
        return await _calculate(arguments["expression"])
    elif name == "recent_events":
        return await _recent_events(arguments["infoType"], arguments["details"]) #type = news/weather, details = location/news topic

    else:
        raise ValueError(f"Unknown tool: {name}")


# ── Search implementation ─────────────────────────────────────────────────────
# async def _web_search(query: str) -> list[types.TextContent]:
#     results = _ddg_search(query)
#     from_where = "ddg"
#     # Tavily fallback if DDG failed
#     if not results and TAVILY_API_KEY:
#         results = _tavily_search(query)
#         from_where = "tav"
    
#     log.info("web_search | source=%s | results=%s", from_where, results)

#     if not results:
#         return [types.TextContent(type="text", text="No results found.")]

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

#unused duck duck go search
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
        url_result = r.get("url", url)
        content = r.get("content", "")
        failed = resp.get("failed_results", [])
        if failed:
            log.warning("fetch_webpage | failed_results=%s", failed)
        # truncate for small models
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

# async def _list_files(topic: str) -> list[types.TextContent]: #make it handle a topic to filter by name of file/ contents. also note if by content, what sections are relevant (if file name, we should tell the ai what sections are relevant and maybe some context around the line too)
#     log.info("list_files | listing workspace root")
#     target = WORKSPACE_ROOT
#     lines = []
#     for root, dirs, files in os.walk(target):
#         dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
#         level = Path(root).relative_to(WORKSPACE_ROOT)
#         prefix = str(level) if str(level) != "." else ""
#         lines.append(f"{prefix}/" if prefix else "/")
#         for f in files:
#             lines.append(f"  {f if not prefix else prefix + '/' + f}")
#     return [types.TextContent(type="text", text="\n".join(lines) or "No files found.")]

# ── File tools ────────────────────────────────────────────────────────────────
def _is_sensitive(filename: str, full_path: str | None = None) -> bool:
    name = filename.lower()
    if name in SENSITIVE_FILENAMES or any(p in name for p in SENSITIVE_PATTERNS):
        return True
    if full_path:
        norm = full_path.replace("\\", "/").lower()
        parts = norm.split("/")
        if any(part in SENSITIVE_FILENAMES for part in parts):
            return True
        if any(p in norm for p in SENSITIVE_PATTERNS):
            return True
    return False

def _is_scannable(path: Path, rel_path: str = "") -> bool:
    if path.is_symlink():
        return False
    name = path.name.lower()
    if name != ".env.example" and path.suffix.lower() not in SCANNABLE_EXTENSIONS:
        return False
    if _is_sensitive(path.name, rel_path):
        return False
    return True

def _find_topic_ranges(
    file_lines: list[str], topic: str, context: int = 2
) -> list[tuple[int, int]]:
    """Return merged 1-based [start, end] line ranges where topic appears."""
    n = len(file_lines)
    # hit_indices from enumerate are always ascending — merge is safe
    hit_indices = [i for i, line in enumerate(file_lines) if topic in line.lower()]
    if not hit_indices:
        return []

    ranges: list[tuple[int, int]] = []
    for i in hit_indices:
        lo = max(0, i - context)
        hi = min(n - 1, i + context)
        if ranges and lo <= ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], hi))
        else:
            ranges.append((lo, hi))

    return [(s + 1, e + 1) for s, e in ranges]

def _read_scannable(path: Path, max_bytes: int = MAX_SCAN_BYTES) -> list[str] | None:
    """One open(): size cap, null-byte detection, line streaming. Returns lines or None."""
    try:
        with path.open("rb") as f:
            chunk = f.read(max_bytes + 1)
    except OSError:
        return None
    if len(chunk) > max_bytes or b"\x00" in chunk[:8192]:
        return None
    return chunk.decode(errors="replace").splitlines()

def _has_symlink_component(path: Path) -> bool:
    for parent in reversed(path.parents):
        if parent.is_symlink():
            return True
    return path.is_symlink()

async def _list_files(topic: str | None = None) -> list[types.TextContent]:
    log.info("list_files | listing workspace root | topic=%s", topic)
    topic_lower = topic.lower() if topic else None
    lines = []

    for root, dirs, files in os.walk(WORKSPACE_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        level = Path(root).relative_to(WORKSPACE_ROOT)
        prefix = str(level) if str(level) != "." else ""
        section_header = f"{prefix}/" if prefix else "/"
        section_lines = []

        for f in files:
            abs_path = Path(root) / f
            rel_path = f if not prefix else f"{prefix}/{f}"

            # symlink guard (files — dirs are handled by dirs[:] filter above)
            if abs_path.is_symlink():
                continue

            # no topic filter — just list everything (skip sensitives still)
            if topic_lower is None:
                if not _is_sensitive(f, rel_path):
                    section_lines.append(f"  {rel_path}")
                continue

            # topic set: filename match
            if topic_lower in f.lower():
                section_lines.append(f"  {rel_path}  [match: filename]")
                continue

            # topic set: content match (only for scannable files)
            if not _is_scannable(abs_path, rel_path):
                continue
            file_lines = _read_scannable(abs_path)
            if file_lines is None:
                continue
            hit_ranges = _find_topic_ranges(file_lines, topic_lower, context=2)

            if hit_ranges:
                range_strs = ", ".join(f"{s}-{e}" for s, e in hit_ranges)
                section_lines.append(f"  {rel_path}  [match: lines {range_strs}]")

        if section_lines:
            lines.append(section_header)
            lines.extend(section_lines)

    return [types.TextContent(type="text", text="\n".join(lines) or "No files found.")]

async def _read_code_skeleton(path: str) -> list[types.TextContent]:
    log.info("read_code_skeleton | path=%s", path)
    raw = WORKSPACE_ROOT / path
    if _has_symlink_component(raw):
        return [types.TextContent(type="text", text="Access denied: symlinks not allowed.")]
    target = raw.resolve()

    try:
        target.relative_to(WORKSPACE_ROOT)
    except ValueError:
        return [types.TextContent(type="text", text="Access denied.")]

    if target.is_symlink():
        return [types.TextContent(type="text", text="Access denied: symlinks not allowed.")]

    if _is_sensitive(target.name, path):
        return [types.TextContent(type="text", text="Access denied: sensitive file.")]

    try:
        size = target.stat().st_size
    except FileNotFoundError:
        return [types.TextContent(type="text", text=f"File not found: {path}")]

    try:
        with target.open("r", encoding="utf-8", errors="replace") as f:
            source = f.read()
    except Exception as e:
        return [types.TextContent(type="text", text=f"Error reading file: {e}")]
        
    ext = target.suffix.lower()
    if ext == ".py":
        skeleton = _get_python_skeleton(source, path)
    else:
        skeleton = _get_generic_skeleton(source, path)
        
    return [types.TextContent(type="text", text=skeleton)]

def _get_python_skeleton(source_code: str, file_path: str) -> str:
    try:
        tree = ast.parse(source_code)
        skeleton = [f"### Skeleton for: {file_path} ###\n"]

        def format_function(node, indent=0):
            prefix = " " * indent
            is_async = isinstance(node, ast.AsyncFunctionDef)
            
            stub_kwargs = {
                "name": node.name, "args": node.args, 
                "body": [ast.Pass()], "decorator_list": [], "returns": node.returns
            }
            stub = ast.AsyncFunctionDef(**stub_kwargs) if is_async else ast.FunctionDef(**stub_kwargs)
            
            try:
                sig = ast.unparse(stub).replace("\n    pass", " ...")
            except AttributeError:
                sig = f"{'async def ' if is_async else 'def '}{node.name}(...): ..."
                
            skeleton.append(f"{prefix}{sig}")
            
            doc = ast.get_docstring(node)
            if doc:
                first_line = doc.strip().split("\n")[0]
                skeleton.append(f"{prefix}    \"\"\"{first_line}...\"\"\"")

        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                try:
                    skeleton.append(ast.unparse(node))
                except AttributeError:
                    pass
            
            elif isinstance(node, ast.ClassDef):
                skeleton.append(f"\nclass {node.name}:")
                doc = ast.get_docstring(node)
                if doc:
                    first_line = doc.strip().split("\n")[0]
                    skeleton.append(f"    \"\"\"{first_line}...\"\"\"")
                    
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        format_function(item, indent=4)
                        
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                format_function(node, indent=0)

        return "\n".join(skeleton)

    except SyntaxError:
        return f"Error: {file_path} contains invalid Python syntax."
    except Exception as e:
        return f"Error parsing {file_path}: {e}"

def _get_generic_skeleton(source_code: str, file_path: str) -> str:
    skeleton = [f"### Skeleton for: {file_path} ###\n"]
    lines = source_code.splitlines()
    
    for i, line in enumerate(lines):
        line = line.rstrip()
        if len(line) > 200:
            continue
        
        stripped = line.lstrip()
        
        # Skip control flow structures
        if stripped.startswith(('if ', 'if(', 'for ', 'for(', 'while ', 'while(', 
                               'switch ', 'switch(', 'else', 'catch ', 'catch(', 'try')):
            continue
        
        # Skip pure closing braces
        if stripped == '}' or stripped == '};':
            continue
        
        # Imports/requires
        if any(stripped.startswith(kw) for kw in ['import ', 'require(', 'using ', 'include ', 'from ']):
            skeleton.append(f"{i+1}: {line}")
            continue
        
        # Class/interface/struct/enum declarations
        if any(f' {kw} ' in line for kw in ['class ', 'interface ', 'struct ', 'enum ', 'trait ']):
            skeleton.append(f"{i+1}: {line}")
            continue
        
        # Function/method declarations: has parens AND ends with brace
        if '(' in line and ')' in line and '{' in line:
            skeleton.append(f"{i+1}: {line}")
            continue
    
    if len(skeleton) == 1:
        return f"### Skeleton for: {file_path} ###\n(No classes or functions detected)"
        
    return "\n".join(skeleton)

async def _read_file(path: str, start: int = 1, count: int | None = None) -> list[types.TextContent]:
    log.info("read_file | path=%s start=%s count=%s", path, start, count)
    raw = WORKSPACE_ROOT / path
    if _has_symlink_component(raw):
        return [types.TextContent(type="text", text="Access denied: symlinks not allowed.")]
    target = raw.resolve()

    try:
        target.relative_to(WORKSPACE_ROOT)
    except ValueError:
        return [types.TextContent(type="text", text="Access denied.")]

    if target.is_symlink():
        return [types.TextContent(type="text", text="Access denied: symlinks not allowed.")]

    if _is_sensitive(target.name, path):
        return [types.TextContent(type="text", text="Access denied: sensitive file.")]

    try:
        size = target.stat().st_size
    except FileNotFoundError:
        return [types.TextContent(type="text", text=f"File not found: {path}")]

    with target.open("rb") as f:
        raw = f.read(MAX_SCAN_BYTES)
    truncated_by_size = size > MAX_SCAN_BYTES
    all_lines = raw.decode(errors="replace").splitlines()
    total = len(all_lines)

    if total == 0:
        return [types.TextContent(type="text", text=f"{path}: empty file.")]
    if count is not None and count <= 0:
        return [types.TextContent(type="text", text="count must be > 0.")]

    start = max(1, min(start, total))
    end = (start + count - 1) if count is not None else total
    end = min(end, start + MAX_FILE_LINES - 1, total)

    selected = all_lines[start - 1 : end]
    was_clamped = end < total and (count is None or start + count - 1 > end)

    if truncated_by_size:
        header = f"Contents of {path} (lines {start}-{end}, file exceeds size limit — prefix only):\n\n"
    else:
        header = f"Contents of {path} (lines {start}-{end} of {total}):\n\n"    
    
    content = "\n".join(selected)

    if was_clamped or truncated_by_size:
        content += f"\n\n[... truncated at line {end} of {total}{'+ (file exceeds size limit)' if truncated_by_size else ''}]"

    return [types.TextContent(type="text", text=header + content)]

#  "start": {"type": "string", "description": "The first line of the section to read from (inclusive)."}
#  "count": {"type": "string", "description": "How many lines to read from the starting line (includes starting line)."}
# async def _read_file(path: str, start: int, count: int) -> list[types.TextContent]: 
#     log.info("read_file | path=%s", path)
#     target = (WORKSPACE_ROOT / path).resolve()
#     try:
#         target.relative_to(WORKSPACE_ROOT)
#     except ValueError:
#         return [types.TextContent(type="text", text="Access denied.")]

#     if target.is_symlink():
#         return [types.TextContent(type="text", text="Access denied: symlinks not allowed.")]

#     try:
#         target.resolve(strict=True)
#     except FileNotFoundError:
#         return [types.TextContent(type="text", text=f"File not found: {path}")]

#     lines = target.read_text(errors="replace").splitlines()
#     truncated = len(lines) > MAX_FILE_LINES
#     content = "\n".join(lines[:MAX_FILE_LINES])
#     if truncated:
#         content += f"\n\n[... truncated at {MAX_FILE_LINES} lines]"

#     return [types.TextContent(type="text", text=f"Contents of {path}:\n\n{content}")]
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
            return await _gnews(details)
        elif infoType == "weather":
            # return _weather(details)
            return await _weather_simple(details)

    except Exception as e:
        return [types.TextContent(type="text", text=f"failed getting info on {infoType}': {e}")]

async def _weather_simple(city: str) -> str:
    try:
        url = f"https://wttr.in/{city}"
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
            return f"{hour_data['tempC']}°C & {hour_data['weatherDesc'][0]['value']}"
        return [
            types.TextContent(
                type="text",
                text=(
                    f"Weather for {city}:\n"
                    f"NOW: {curr['temp_C']}°C, {curr['weatherDesc'][0]['value']}\n"
                    f"+6H:  {get_hourly_data(curr_hour + 6)}\n"
                    f"+12H: {get_hourly_data(curr_hour + 12)}\n"
                    f"+18H: {get_hourly_data(curr_hour + 18)}\n"
                    f"+24H: {get_hourly_data(curr_hour + 24)}"
                )
            )
        ]   
    except Exception as e:
        return [
            types.TextContent(type="text", text=f"Error: {e}")
        ]        
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
    query_clean = query.strip().lower()

    general_categories = {
        "technology", "sports", "business", "health",
        "science", "entertainment", "world", "nation",
        "general"
    }

    use_headlines = query_clean in general_categories

    if use_headlines:
        url = "https://gnews.io/api/v4/top-headlines"
        params = {
            "category": query_clean if query_clean in general_categories else "general",
            "lang": "en",
            "max": 5,
            "token": GNEWS_API_KEY
        }
    else:
        url = "https://gnews.io/api/v4/search"
        params = {
            "q": query,
            "lang": "en",
            "max": 10,
            "token": GNEWS_API_KEY
        }

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, params=params)
        data = r.json()

    articles = data.get("articles", [])

    text = "\n\n".join(
        f"{a.get('title','')}\n{a.get('url','')}\n{a.get('description','')}"
        for a in articles
    )

    return [
        types.TextContent(type="text", text=text)
    ]

# ── Entry point ───────────────────────────────────────────────────────────────
async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())
