"""
tools/registry.py — MCP tool registry.
Owns the MCP Server instance. Registers list_tools and call_tool handlers
and delegates to individual tool modules. Add new tools here.
"""
import logging
from mcp.server import Server
from mcp import types

from tools.web import _web_search, _fetch_webpage
from tools.files import _list_files, _read_file, _read_code_skeleton
from tools.calculate import _calculate
from tools.events import _recent_events
from tools.emails import _draft_email
from tools.search import _search_semantic
from tools.memory import _manage_memory

log = logging.getLogger(__name__)

app = Server("local-search-mcp")


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
                "The available operations are: addition, subtraction, multiplication, division, modulo, and power. "
                "Remember you can use fractional powers to perform roots. "
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
        types.Tool(
            name="draft_email",
            description="Draft an email by opening Thunderbird's compose window with pre-filled fields. Use this when the user asks to draft or compose an email.",
            inputSchema={
                "type": "object",
                "properties": {
                    "to": {
                        "type": "string", 
                        "description": "Recipient email address (e.g., 'someone@example.com')"
                    },
                    "subject": {
                        "type": "string", 
                        "description": "The subject line of the email"
                    },
                    "body": {
                        "type": "string", 
                        "description": "The plain text body of the email"
                    }
                },
                "required": ["body"]
            }
        ),
        types.Tool(
            name="search_semantic",
            description=(
                "Search the workspace using conceptual/semantic embeddings. "
                "Use this for high-level conceptual searches, like 'how does the auth system work' "
                "or 'where is the database connection initialized'. "
                "Returns a list of matching code locations. "
                "Always use this when keyword-based `list_files(topic='...')` is too specific or fails."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The conceptual search query."
                    },
                    "k": {
                        "type": "integer",
                        "description": "Number of results to return. Defaults to 5."
                    }
                },
                "required": ["query"]
            }
        ),
        types.Tool(
            name="manage_memory",
            description=(
                "Write, delete, or edit agent-managed memories. "
                "Use 'write' to save new long-term info (max 10 slots). "
                "Use 'delete' to remove a memory by its meaning or content. "
                "Use 'edit' to replace a memory with new content. "
                "Semantic search is used to find the best match for 'delete' and 'edit'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["write", "delete", "edit", "clear_all"],
                        "description": "The action to perform."
                    },
                    "content": {
                        "type": "string",
                        "description": "The content to remember (for 'write') or the search target (for 'delete' or 'edit')."
                    },
                    "new_content": {
                        "type": "string",
                        "description": "The new content to replace the matched memory (required for 'edit')."
                    }
                },
                "required": ["action"]
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
        return await _recent_events(arguments["infoType"], arguments["details"])
    elif name == "draft_email":
        return await _draft_email(arguments.get("to", ""), arguments.get("subject", ""), arguments.get("body", ""))
    elif name == "search_semantic":
        return await _search_semantic(arguments["query"], arguments.get("k", 5))
    elif name == "manage_memory":
        result = await _manage_memory(arguments["action"], arguments.get("content"), arguments.get("new_content"))
        return [types.TextContent(type="text", text=result)]
    else:
        raise ValueError(f"Unknown tool: {name}")
