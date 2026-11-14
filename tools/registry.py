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
from tools.edit import resolve_edit, ReadOnlyPathError
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
                "NEWS IS NOT REAL-TIME: the news feed lags behind by "
                "roughly 12 hours, so the very latest breaking stories (anything within the last several "
                "hours) may not appear yet. When reporting news, treat it as recent-but-delayed and say so "
                "if the user is asking about something that may have just happened; for truly breaking, "
                "up-to-the-minute events use web_search instead. "
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
                "Persistent keyed store of the USER'S PREFERENCES ABOUT THEMSELVES and how they want help. "
                "Each preference is a key -> value pair, e.g. key='preferred_language', value='Python'. "
                "Writing an existing key OVERWRITES its value (upsert); you do not need to delete first to change one. "
                "USE ONLY FOR: stable facts/preferences about the user — favorite editor or language, formatting/style preferences, "
                "communication style, units, recurring personal habits, or something the user explicitly asks to be remembered. "
                "NEVER USE FOR: feature requests, things to build, project TODOs or backlog items (e.g. 'add a graph to search', "
                "'build a token estimator'), code, file contents, search results, task outputs, or anything temporary. "
                "If the user is describing something they want built or changed in the project, that is NOT a preference — do not store it here."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["write", "delete", "clear_all"],
                        "description": "'write' to add or overwrite a preference by key, 'delete' to remove one by key, 'clear_all' to wipe agent-managed preferences."
                    },
                    "key": {
                        "type": "string",
                        "description": "Short stable snake_case identifier for the preference, e.g. 'preferred_language', 'editor', 'response_style'. Required for 'write' and 'delete'. Writing an existing key overwrites it."
                    },
                    "value": {
                        "type": "string",
                        "description": "The preference value to store (required for 'write')."
                    }
                },
                "required": ["action"]
            }
        ),
        types.Tool(
            name="propose_edit",
            description=(
                "Propose a single file edit or creation. One call edits one file with one action. "
                "Always use read_file first to check current content before editing an existing file.\n"
                "\n"
                "HOW IT WORKS: 'anchor' is the EXACT existing text the action operates on. "
                "For 'replace', the anchor text is deleted and 'content' is put in its place. "
                "So 'content' must be the rewritten version of the anchored text — NOT a new unrelated block, "
                "and NOT text you want to appear elsewhere. Think of it as: old text = anchor, new text = content.\n"
                "\n"
                "REPLACE EXAMPLE — to change 'reducing memory usage by about 50%' to '...by about 60%':\n"
                "  anchor:  \"reducing memory usage by about 50%\"\n"
                "  content: \"reducing memory usage by about 60%\"\n"
                "The anchor and content cover THE SAME span of text. Do NOT put a 2-word anchor and a whole paragraph as content — "
                "that deletes the 2 words and dumps the paragraph mid-sentence.\n"
                "\n"
                "Actions:\n"
                "  - 'replace': delete the anchor text, put 'content' in its place. content = rewrite of the anchor.\n"
                "  - 'delete': remove the anchor text entirely. Use this to drop a redundant sentence, line, or block. 'content' is ignored.\n"
                "  - 'insert_before': keep the anchor, add 'content' as new line(s) immediately before it.\n"
                "  - 'insert_after': keep the anchor, add 'content' as new line(s) immediately after it.\n"
                "  - 'create': make a new file; anchor is ignored, content is the ENTIRE file.\n"
                "\n"
                "Anchor rules:\n"
                "  - The anchor must match the file text (whitespace and line breaks are ignored, so a single-line anchor may span wrapped lines; "
                "markdown decoration like **bold** and smart quotes are also tolerated).\n"
                "  - Keep anchors focused: a unique phrase (roughly 5-20 words), just long enough to appear exactly once.\n"
                "  - If the anchor appears more than once the edit is rejected — the error lists each occurrence with surrounding context so you can "
                "add a few words to target one, OR set replace_all=true to change EVERY occurrence at once.\n"
                "  - If the anchor is not found, the error shows the closest actual text in the file — copy it EXACTLY as your next anchor.\n"
                "\n"
                "replace_all (optional, replace/delete only): when true, applies the action to EVERY occurrence of the anchor instead of requiring "
                "a unique match. Use deliberately — it changes all matches at once.\n"
                "\n"
                "To make multiple DIFFERENT changes, call propose_edit once per change."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file, relative to the aiWorkspace folder."
                    },
                    "action": {
                        "type": "string",
                        "enum": ["replace", "delete", "insert_before", "insert_after", "create"],
                        "description": "The type of edit to perform."
                    },
                    "anchor": {
                        "type": "string",
                        "description": "The EXACT existing text to act on. For 'replace' this is the text that gets deleted; for 'delete' it is the text removed; for insert it is the reference point. A unique phrase (~5-20 words), appearing exactly once. Required for replace/delete/insert_before/insert_after. Ignored for create."
                    },
                    "content": {
                        "type": "string",
                        "description": "For 'replace': the rewritten version of the anchored text that takes its place (same span, edited). For insert: the new line(s) to add. For 'create': the entire file contents. Ignored for 'delete'."
                    },
                    "replace_all": {
                        "type": "boolean",
                        "description": "Optional. For 'replace'/'delete' only. When true, applies the action to EVERY occurrence of the anchor instead of requiring a unique match. Default false."
                    },
                    "summary": {
                        "type": "string",
                        "description": "A very short description of the change (max ~15 words)."
                    }
                },
                "required": ["path", "action", "summary"]
            }
        ),
        types.Tool(
            name="subagent_search",
            description=(
                "Delegate a SINGLE, focused web-research question to a lightweight research "
                "worker. The worker searches the web on its own and returns a short digest of "
                "what it found — you get only the digest, not the raw pages, so it keeps your "
                "context clean. Use this to offload a self-contained fact-finding lookup "
                "(e.g. 'What license is X released under?', 'current stable version of Y').\n"
                "\n"
                "IMPORTANT — one focus per call. The worker is small and reliably answers ONE "
                "focused question, but CANNOT synthesize multi-part answers. For a complex or "
                "multi-part research request, DECOMPOSE it yourself into several simple "
                "single-focus subagent_search calls (one fact each) and combine the digests in "
                "your own answer — do NOT send one broad multi-part query. "
                "Prefer recent_events for live news/weather. This worker is read-only: it "
                "cannot edit files, send email, or call other tools."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "A single, focused research question for the worker to answer. Keep it to one fact/topic."
                    }
                },
                "required": ["query"]
            }
        ),
        types.Tool(
            name="subagent_context",
            description=(
                "Delegate a SINGLE, focused workspace-gathering task to a lightweight research "
                "worker. The worker searches and reads local workspace files on its own and "
                "returns a short digest — you get only the digest, not the raw file dumps, so it "
                "keeps your context clean. Use this to offload a self-contained 'find / summarize "
                "X in the project' task (e.g. 'Which file defines the Ollama payload builder and "
                "what does it do?', 'Summarize what greeting.txt contains').\n"
                "\n"
                "IMPORTANT — one focus per call. The worker is small and reliably answers ONE "
                "focused task, but CANNOT synthesize multi-part answers. For a complex or "
                "multi-part request, DECOMPOSE it yourself into several simple single-focus "
                "subagent_context calls (one thing each) and combine the digests in your own "
                "answer — do NOT send one broad multi-part task. This worker is read-only: it "
                "can search and read files but cannot edit them or call other tools. For a file "
                "you intend to EDIT, read it yourself with read_file first (do not edit based "
                "only on a digest)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "A single, focused workspace task (find/summarize one thing). Keep it to one topic."
                    }
                },
                "required": ["task"]
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
        result = await _manage_memory(arguments["action"], arguments.get("key"), arguments.get("value"))
        return [types.TextContent(type="text", text=result)]
    elif name == "propose_edit":
        try:
            # Single edit built from the flat arguments.
            edit = {
                "path": arguments.get("path", ""),
                "action": arguments.get("action", "replace"),
                "anchor": arguments.get("anchor", ""),
                "content": arguments.get("content", ""),
                "replace_all": bool(arguments.get("replace_all", False)),
            }

            try:
                preview = resolve_edit(WORKSPACE_ROOT, edit)
            except ReadOnlyPathError as e:
                # Terminal policy denial — make clear it must not be retried.
                return [types.TextContent(type="text", text=(
                    f"REJECTED (read-only): {str(e)} Do not retry this edit."
                ))]
            except ValueError as e:
                return [types.TextContent(type="text", text=f"REJECTED: Edit failed: {str(e)}")]

            # Wrap the single edit in a one-element array so downstream
            # consumers that still expect the batch shape keep working.
            edit_results = [{
                "index": 0,
                "path": preview["path"],
                "action": edit["action"],
                "anchor": edit["anchor"],
                "content": edit["content"],
                "old_content": preview["old_content"],
                "new_content": preview["new_content"],
                "target_path": preview["target_path"],
            }]

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
    elif name in ("subagent_search", "subagent_context"):
        # Intercepted in agent/loop.py (needs conversation_id + tool_call_id).
        # Dead no-op stub — exists only so the schema is advertised via
        # list_tools(). Mirrors the `plan` stub above.
        return [types.TextContent(type="text", text="OK (handled by agent)")]
    else:
        raise ValueError(f"Unknown tool: {name}")
