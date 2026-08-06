"""
tools/registry.py — MCP tool registry.
Owns the MCP Server instance. Registers list_tools and call_tool handlers
and delegates to individual tool modules. Add new tools here.
"""
import logging
from mcp.server import Server
from mcp import types

from tools.web import _web_search, _fetch_webpage
from tools.files import _list_files, _read_file, _read_code_skeleton, _list_files_by_glob, _is_glob_pattern
from tools.calculate import _calculate
from tools.events import _recent_events
from tools.emails import _draft_email
from tools.search import _search_semantic
from tools.memory import _manage_memory
from tools.edit import validate_and_preview_edit
from tools.files import WORKSPACE_ROOT
import json

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
                "List or search files in the workspace. "
                "To list ALL files, omit 'query' or pass '*'. "
                "To filter by extension, use glob patterns like '*.py', '*.txt', '**/*.js'. "
                "To search file names and content by keyword, pass a plain word or phrase (e.g. 'greet', 'import math'). "
                "Results include file names and matching content snippets. "
                "Always start here to discover what files exist before trying to read them."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "A glob pattern (e.g. *, *.py, **/*.txt) to filter files, or a keyword/phrase to search content. Omit to list all files."
                    }
                },
                "required": []
            }
        ),
        types.Tool(
            name="read_file",
            description=(
                "Read the contents of a file. "
                "Pass the exact file name with extension (e.g. 'draft.txt', 'simpleHelper.py'). "
                "Use search_workspace first to discover exact file names if unsure. "
                "Set mode to 'skeleton' to get a structural overview of code files."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Exact file name with extension, relative to workspace root (e.g. 'draft.txt', 'utils/helpers.py')."},
                    "mode": {"type": "string", "enum": ["content", "skeleton"], "description": "Whether to read the full content or just the structural outline."},
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
            name="save_user_preference",
            description=(
                "Persistent personal memory ONLY. "
                "Use this tool only for long-term user preferences, identity details, habits, or explicitly requested remembered information. "
                "Examples: favorite editor, preferred coding language, recurring workflows, personal preferences. "
                "Never use for project code, documentation, implementations, file contents, search results, temporary findings, task outputs, or general knowledge. "
                "Do not use this tool unless the user clearly wants something remembered across conversations."            ),
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
        types.Tool(
            name="propose_edit",
            description=(
                "Propose file edits or creations. Each edit targets one file with one action. "
                "Always use read_file first to check current content before editing an existing file. "
                "Pass an 'edits' array where EVERY entry must have: path, action, anchor, content. "
                "Actions:\n"
                "  - 'replace': find the anchor text and replace it with content.\n"
                "  - 'insert_before': find the anchor text and insert content on a new line before it.\n"
                "  - 'insert_after': find the anchor text and insert content on a new line after it.\n"
                "  - 'create': create a new file; anchor is ignored, content is the full file.\n"
                "IMPORTANT anchor rules:\n"
                "  - Keep anchors SHORT: one line or a unique phrase (5-20 words). Never use multi-line anchors.\n"
                "  - The anchor must appear exactly once in the file.\n"
                "  - Use a unique fragment, not a whole paragraph.\n"
                "  - Example good anchor: 'reducing memory usage by about 50%'\n"
                "  - Example bad anchor: the entire paragraph (will fail to match due to whitespace differences).\n"
                "For multiple changes to one file, use multiple edit entries with the same path but different short anchors."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "edits": {
                        "type": "array",
                        "description": "List of edits to propose. Each is independently approved or rejected.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {
                                    "type": "string",
                                    "description": "Path to the file, relative to the aiWorkspace folder."
                                },
                                "action": {
                                    "type": "string",
                                    "enum": ["replace", "insert_before", "insert_after", "create"],
                                    "description": "The type of edit to perform."
                                },
                                "anchor": {
                                    "type": "string",
                                    "description": "Text to locate in the file. Used by replace, insert_before, insert_after. Ignored for create."
                                },
                                "content": {
                                    "type": "string",
                                    "description": "The new text: replacement text, insertion text, or full file content for create."
                                }
                            },
                            "required": ["path", "action", "content"]
                        },
                        "minItems": 1
                    },
                    "summary": {
                        "type": "string",
                        "description": "A very short description of the overall change (max ~15 words)."
                    }
                },
                "required": ["edits", "summary"]
            }
        ),
        types.Tool(
            name="plan",
            description=(
                "Manage a step-by-step plan for the current task. "
                "Use 'create' at the start of multi-step tasks. "
                "Use 'update' to mark steps done/failed or add new steps. "
                "Only one plan can be active per conversation — 'create' replaces any existing plan."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create", "update"],
                        "description": "Whether to create a new plan or update the existing one."
                    },
                    "goal": {
                        "type": "string",
                        "description": "Short description of what the plan accomplishes (required for 'create')."
                    },
                    "steps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Ordered list of step descriptions (required for 'create').",
                        "minItems": 1
                    },
                    "step_index": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Zero-based index of the step to update (for 'update')."
                    },
                    "status": {
                        "type": "string",
                        "enum": ["done", "failed"],
                        "description": "New status for the step at step_index (for 'update')."
                    },
                    "new_steps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Additional steps to append to the plan (for 'update')."
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
        
        # Handle glob/wildcard patterns (e.g. *.py, **, test_*)
        if _is_glob_pattern(query):
            return _list_files_by_glob(query)
        
        # Run both keyword and semantic search
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

        # If nothing found at all, add a hint
        if not keyword_results and not semantic_results:
            combined_text += "\n\nHint: try calling search_workspace with no query or with '*' to list all files."
        
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
    elif name == "save_user_preference":
        result = await _manage_memory(arguments["action"], arguments.get("content"), arguments.get("new_content"))
        return [types.TextContent(type="text", text=result)]
    elif name == "propose_edit":
        try:
            edits = arguments.get("edits", [])
            if not edits:
                return [types.TextContent(type="text", text="REJECTED: 'edits' array is required and must not be empty.")]

            # Validate and compute preview for each individual edit
            edit_results = []
            for i, edit in enumerate(edits):
                try:
                    preview = validate_and_preview_edit(WORKSPACE_ROOT, edit)
                    edit_results.append({
                        "index": i,
                        "path": preview["path"],
                        "action": edit.get("action", "replace"),
                        "anchor": edit.get("anchor", ""),
                        "content": edit.get("content", ""),
                        "old_content": preview["old_content"],
                        "new_content": preview["new_content"],
                        "target_path": preview["target_path"],
                    })
                except ValueError as e:
                    return [types.TextContent(type="text", text=f"REJECTED: Edit #{i+1} failed: {str(e)}")]

            result = {
                "status": "pending_approval",
                "edits": edit_results,
                "summary": arguments.get("summary", "")
            }
            return [types.TextContent(type="text", text=json.dumps(result))]
        except Exception as e:
            return [types.TextContent(type="text", text=f"REJECTED: {str(e)}")]
    elif name == "plan":
        # Plan tool is intercepted in agent.py (needs conversation_id).
        # If it somehow reaches here, return a no-op acknowledgment.
        return [types.TextContent(type="text", text="OK (handled by agent)")]
    else:
        raise ValueError(f"Unknown tool: {name}")
