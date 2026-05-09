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
            name="web_tool",
            description=(
                "Search the web or fetch a specific webpage. "
                "Use 'search' to find information, or 'fetch' to extract text from a specific URL. "
                "Note: some major news sites block content extraction."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["search", "fetch"]},
                    "target": {"type": "string", "description": "The search query (if action=search) or full URL (if action=fetch)."}
                },
                "required": ["action", "target"]
            }
        ),
        types.Tool(
            name="search_workspace",
            description=(
                "Search the workspace for files. "
                "If a query is provided, it returns both exact keyword matches and semantic conceptual matches. "
                "If no query is provided, it simply lists all files."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search term or conceptual query. Omit to list all files."
                    }
                },
                "required": []
            }
        ),
        types.Tool(
            name="read_file",
            description=(
                "Read the contents or structural outline of a file. "
                "Pass a relative path like 'server.py' or 'utils/helpers.py'. "
                "Set mode to 'skeleton' to get an overview of the file's architecture first."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file, relative to workspace root."},
                    "mode": {"type": "string", "enum": ["content", "skeleton"], "description": "Whether to read the full content or just the outline."},
                    "start": {"type": "integer", "minimum": 1, "description": "1-based line number to start reading from (inclusive). Defaults to 1."},
                    "count": {"type": "integer", "minimum": 1, "description": "Number of lines to read starting from 'start'."}
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
    if name == "web_tool":
        if arguments["action"] == "search":
            return await _web_search(arguments["target"])
        elif arguments["action"] == "fetch":
            return await _fetch_webpage(arguments["target"])
        else:
            raise ValueError(f"Unknown web_tool action: {arguments.get('action')}")
    elif name == "search_workspace":
        query = arguments.get("query")
        if not query:
            return await _list_files(None)
        
        # Run both
        keyword_results = await _list_files(query)
        semantic_results = await _search_semantic(query, 3)
        
        combined_text = "=== Keyword Matches ===\n"
        if keyword_results:
            combined_text += keyword_results[0].text
        else:
            combined_text += "No keyword matches found."
            
        combined_text += "\n\n=== Semantic Matches ===\n"
        if semantic_results:
            combined_text += semantic_results[0].text
        else:
            combined_text += "No semantic matches found."
        
        return [types.TextContent(type="text", text=combined_text)]
    elif name == "read_file":
        if arguments.get("mode") == "skeleton":
            return await _read_code_skeleton(arguments["path"])
        return await _read_file(arguments["path"], arguments.get("start", 1), arguments.get("count", None))
    elif name == "calculate":
        return await _calculate(arguments["expression"])
    elif name == "recent_events":
        return await _recent_events(arguments["infoType"], arguments["details"])
    elif name == "draft_email":
        return await _draft_email(arguments.get("to", ""), arguments.get("subject", ""), arguments.get("body", ""))
    elif name == "manage_memory":
        result = await _manage_memory(arguments["action"], arguments.get("content"), arguments.get("new_content"))
        return [types.TextContent(type="text", text=result)]
    else:
        raise ValueError(f"Unknown tool: {name}")
