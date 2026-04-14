"""
server.py — FastAPI backend
- Acts as MCP client (spawns mcp_server.py, calls its tools)
- Runs the Ollama agentic loop
- Streams responses to the browser via SSE
"""

import asyncio
import json
import os
import subprocess
import sys
import threading
from contextlib import asynccontextmanager
from typing import AsyncGenerator
import uuid
# Windows requires this for asyncio subprocess support
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import logging
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()
logging.basicConfig(
    filename="logs.txt",
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CONFIG — edit these or set as environment variables
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CONFIG = {
    "use_groq":         os.getenv("USE_GROQ", "false").lower() == "true",
    "groq_api_key":     os.getenv("GROQ_API_KEY", ""),
    "groq_model":       os.getenv("GROQ_MODEL", "llama-3.1-8b-instant"),
    "ollama_base_url":  os.getenv("OLLAMA_URL", "http://localhost:11434"),
    "model":            os.getenv("MODEL", "gemma4:e2b"),    
    # "model":            os.getenv("MODEL",              "qwen3.5:9b"),
    # "model":            os.getenv("MODEL",              "gemma4"),
    "max_searches":     int(os.getenv("MAX_SEARCHES",   "2")),    # per turn cap
    "max_tokens":       int(os.getenv("MAX_TOKENS",     "1024")),
    "temperature":      float(os.getenv("TEMPERATURE",  "0.7")),
    "mcp_server_cmd":   os.getenv("MCP_CMD", f"{sys.executable} mcp_server.py"),
    "system_prompt": os.getenv("SYSTEM_PROMPT", (
        "You are a helpful personal assistant. Make sure to communicate with sufficient detail without adding filler. "

        "Use web_search only when the question requires current information — news, prices, events, "
        "or anything that may have changed recently. For casual conversation, stable concepts, or "
        "anything you can answer confidently, respond directly without searching. "
        "Don't search just to confirm something you already know with high confidence. "

        "When you do search, don't stop at snippets if the topic warrants more depth — use "
        "fetch_webpage on the most relevant result to get fuller detail. Chain searches if needed. "
        "Always report what you found, even if incomplete, and note any gaps in the information. "
        "Mention where the information came from. "

        "You have read-only access to a file workspace. "
        "If the user asks you to review, debug, or improve existing code or files, call list_files first to see what's available, then read_file on relevant files before responding. "
        "If the user asks you to generate, write, or explain code from scratch, just do it directly without checking the workspace. "
        "Never attempt to write, edit, or delete files — only read and suggest changes in chat."        
    )),
}


#key options
MAX_HISTORY = 10
SUMMARIZE_THRESHOLD = 8      # summarize when history exceeds this many messages
SUMMARIZE_KEEP_LAST = 3      # keep this many recent messages unsummarized

STARTUP_TOKEN = str(uuid.uuid4())

#coding options
CODING_PHRASES = [
    "write a", "write me", "generate a", "create a", "make a",
    "fix this", "debug this", "help me code", "how do i implement",
    "whats wrong with", "can you code", "show me how to",
]

CODING_KEYWORDS = {
    "debug", "refactor", "algorithm", "recursion", "compile",
    "stackoverflow", "syntax error", "runtime error", "null pointer",
    "segfault", "dockerfile", "kubernetes", "git", "npm", "pip",
    "import", "instanceof", "polymorphism", "inheritance",
}
CODING_MODEL = "llama-3.3-70b-versatile"

def _select_groq_model(message: str) -> tuple[str, str]:
    lower = message.lower()
    words = set(lower.split())
    is_coding = bool(words & CODING_KEYWORDS) or any(p in lower for p in CODING_PHRASES)
    
    tool_model = CONFIG["groq_model"]          # always small for tool calls
    answer_model = CODING_MODEL if is_coding else CONFIG["groq_model"]
    return tool_model, answer_model
# near the top of server.py, after load_dotenv()
MEMORY_FILE = Path(__file__).parent / "aiNotes/memory.json"

def load_memory() -> str:
    try:
        if MEMORY_FILE.exists():
            data = json.loads(MEMORY_FILE.read_text())
            if not data:
                return ""
            lines = [f"- {k}: {v}" for k, v in data.items()]
            return "User memory:\n" + "\n".join(lines)
    except Exception:
        pass
    return ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MCP CLIENT — talks to mcp_server.py over stdio
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class MCPClient:
    """
    Minimal MCP client over stdio.
    Uses regular subprocess.Popen + threading to avoid Windows asyncio subprocess issues.
    """

    def __init__(self, cmd: str):
        self.cmd = cmd.split()
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._msg_id = 0

    def start(self):
        """Blocking start — call from a thread or before the event loop if needed."""
        self._proc = subprocess.Popen(
            self.cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
        )
        self._send({
            "jsonrpc": "2.0", "id": self._next_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "local-agent", "version": "0.1"}
            }
        })
        self._recv()  # discard init response
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    def stop(self):
        if self._proc:
            self._proc.terminate()
            self._proc.wait()

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    def _send(self, msg: dict):
        line = json.dumps(msg) + "\n"
        self._proc.stdin.write(line.encode())
        self._proc.stdin.flush()

    def _recv(self) -> dict:
        line = self._proc.stdout.readline()
        return json.loads(line)

    async def call_tool(self, name: str, arguments: dict) -> str:
        """Run blocking MCP call in a thread executor so we don't block the event loop."""
        def _call():
            with self._lock:
                msg_id = self._next_id()
                self._send({
                    "jsonrpc": "2.0", "id": msg_id,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments}
                })
                return self._recv()

        resp = await asyncio.get_event_loop().run_in_executor(None, _call)

        if "error" in resp:
            return f"Tool error: {resp['error']}"

        contents = resp.get("result", {}).get("content", [])
        return "\n".join(c.get("text", "") for c in contents if c.get("type") == "text")

    async def list_tools(self) -> list[dict]:
        def _list():
            with self._lock:
                msg_id = self._next_id()
                self._send({
                    "jsonrpc": "2.0", "id": msg_id,
                    "method": "tools/list", "params": {}
                })
                return self._recv()

        resp = await asyncio.get_event_loop().run_in_executor(None, _list)
        return resp.get("result", {}).get("tools", [])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# APP SETUP
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
mcp = MCPClient(CONFIG["mcp_server_cmd"])
conversation_history: list[dict] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.get_event_loop().run_in_executor(None, mcp.start)
    print(f"[server] MCP client started | model={CONFIG['model']} | max_searches={CONFIG['max_searches']}")
    yield
    await asyncio.get_event_loop().run_in_executor(None, mcp.stop)


app = FastAPI(title="Local MCP Search Agent", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ROUTES
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class ChatRequest(BaseModel):
    message: str


@app.get("/config")
async def get_config():
    """Expose config to the UI so it can show current settings."""
    return {k: v for k, v in CONFIG.items() if k != "mcp_server_cmd"}


@app.post("/config")
async def update_config(updates: dict):
    """Let the UI update runtime config (model, max_searches, temp, etc.)."""
    allowed = {"model", "max_searches", "max_tokens", "temperature", "system_prompt", "use_groq", "groq_model"}    
    for k, v in updates.items():
        if k in allowed:
            CONFIG[k] = v
    return {"status": "ok", "config": {k: CONFIG[k] for k in allowed}}


@app.delete("/history")
async def clear_history():
    conversation_history.clear()
    return {"status": "cleared"}


@app.post("/chat")
async def chat(req: ChatRequest):
    """Main chat endpoint — returns SSE stream."""
    return StreamingResponse(
        _agent_loop(req.message),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

@app.get("/startup-token")
async def startup_token():
    return {"token": STARTUP_TOKEN}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# AGENTIC LOOP
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _sse(event: str, data: str) -> str:
    payload = json.dumps({"event": event, "data": data})
    return f"data: {payload}\n\n"

def _prune_history(history: list[dict], keep_last_n_tool_results: int = 2) -> list[dict]:
    pruned = []
    tool_result_count = 0
    for msg in reversed(history):
        if msg["role"] == "tool":
            tool_result_count += 1
            if tool_result_count > keep_last_n_tool_results:
                pruned.append({**msg, "content": "[pruned]"})
                continue
        pruned.append(msg)
    return list(reversed(pruned))

import traceback

async def _maybe_summarize_history(groq_client) -> None:
    if len(conversation_history) < SUMMARIZE_THRESHOLD:
        return
    
    to_summarize = conversation_history[:-SUMMARIZE_KEEP_LAST]
    keep = conversation_history[-SUMMARIZE_KEEP_LAST:]
    
    summarizable = [m for m in to_summarize if m["role"] in ("user", "assistant") and m.get("content")]
    
    if not summarizable:
        return

    summary_response = await groq_client.chat.completions.create(
        model=CONFIG["groq_model"],
        messages=[
            {"role": "system", "content": "Summarize the following conversation history concisely in 3-5 sentences, preserving key facts and context."},
            {"role": "user", "content": json.dumps(summarizable)}
        ],
        max_completion_tokens=300,
        temperature=0.3,
    )
    
    summary = summary_response.choices[0].message.content
    conversation_history.clear()
    conversation_history.append({"role": "assistant", "content": f"[Previous conversation summary]: {summary}"})
    conversation_history.extend(keep)
    log.info("history summarized | new length=%d", len(conversation_history))

async def _summarize_tool_result(groq_client, tool_name: str, result: str) -> str:
    if len(result) < 300:
        return result
    if tool_name not in ("web_search", "fetch_webpage"):
        return result

    try:
        summary = await groq_client.chat.completions.create(
            model=CONFIG["groq_model"],  # always small model for summarization
            messages=[
                {"role": "system", "content": (
                    "Summarize this search result in 3-4 sentences. "
                    "Preserve all key facts, numbers, dates, URLs, and names. "
                    "Be concise but don't lose important details."
                )},
                {"role": "user", "content": result}
            ],
            max_completion_tokens=200,
            temperature=0.1,
        )
        summarized = summary.choices[0].message.content
        log.info("summarized tool result | %s | %d→%d chars", tool_name, len(result), len(summarized))
        return summarized
    except Exception as e:
        log.warning("summary failed, using raw result | %s", e)
        return result  # fall back to raw if summarization fails

async def _agent_loop(user_message: str) -> AsyncGenerator[str, None]:
    try:
        yield _sse("status", "loop started")

        conversation_history.append({"role": "user", "content": user_message})
        yield _sse("status", "history appended")

        # detect and select model

        tool_model, answer_model = _select_groq_model(user_message)

        if answer_model == CODING_MODEL:
            yield _sse("model_upgrade", CODING_MODEL)
        # Fetch tool schemas from MCP server for Ollama
        mcp_tools = await mcp.list_tools()
        yield _sse("status", f"got {len(mcp_tools)} tools")

        ollama_tools = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["inputSchema"],
                }
            }
            for t in mcp_tools
        ]
        yield _sse("status", f"calling {'groq' if CONFIG['use_groq'] else 'ollama'}")
        
        search_count = 0
        max_searches = CONFIG["max_searches"]

        async with httpx.AsyncClient(timeout=240) as client:
            while True:
                trimmed_history = _prune_history(conversation_history[-MAX_HISTORY:])
                is_final_call = len(trimmed_history) > 0 and trimmed_history[-1]["role"] == "tool"
                current_model = answer_model if is_final_call else tool_model

                # set up groq client fresh each iteration in case use_groq was toggled
                groq_client = None
                if CONFIG["use_groq"]:
                    from groq import AsyncGroq
                    groq_client = AsyncGroq(api_key=CONFIG["groq_api_key"])
                    await _maybe_summarize_history(groq_client)                

                memory = load_memory()
                
                system = CONFIG["system_prompt"]
                if memory:
                    system = system + "\n\n" + memory

                # ── Call llm ───────────────────────────────────────────────────
                if CONFIG["use_groq"]:
                    response = await groq_client.chat.completions.create(
                        model=current_model,
                        messages=[
                            {"role": "system", "content": system},
                            *trimmed_history,
                        ],
                        tools=ollama_tools if search_count < max_searches else [],
                        temperature=CONFIG["temperature"],
                        max_completion_tokens=CONFIG["max_tokens"],
                    )
                    msg = response.choices[0].message
                    tool_calls = msg.tool_calls or []
                    content = msg.content or ""

                    log.info("groq response | content=%s | tool_calls=%s", content, tool_calls)


                    yield _sse("status", "groq responded")
                else:
                    payload = {
                        "model": CONFIG["model"],
                        "messages": [
                            {"role": "system", "content": system},
                            *trimmed_history,
                        ],
                        "tools": ollama_tools if search_count < max_searches else [],
                        "options": {
                            "temperature": CONFIG["temperature"],
                            "num_predict": CONFIG["max_tokens"],
                        },
                        "stream": False,
                    }
                    resp = await client.post(f"{CONFIG['ollama_base_url']}/api/chat", json=payload)
                    yield _sse("status", f"ollama responded: {resp.status_code}")
                    resp.raise_for_status()
                    data = resp.json()
                    msg = data.get("message", {})
                    tool_calls = msg.get("tool_calls", [])
                    content = msg.get("content", "")

                # ── No tool calls → stream final answer ──────────────────────────
                if not tool_calls:
                    conversation_history.append({"role": "assistant", "content": content})
                    for word in content.split(" "):
                        yield _sse("token", word + " ")
                        await asyncio.sleep(0.01)
                    yield _sse("done", json.dumps({
                        "searches_used": search_count,
                        "max_searches": max_searches
                    }))
                    return

                # ── Handle tool calls ─────────────────────────────────────────────
                # Serialize tool_calls to dicts for history (Groq returns objects)
                if CONFIG["use_groq"]:
                    tool_calls_for_history = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments if tc.function.arguments and tc.function.arguments != "null" else "{}"
                            }
                        }
                        for tc in tool_calls
                    ]                
                else:
                    tool_calls_for_history = tool_calls

                conversation_history.append({"role": "assistant", "content": "", "tool_calls": tool_calls_for_history})
                for tc in tool_calls:
                    if CONFIG["use_groq"]:
                        name = tc.function.name
                        raw_args = tc.function.arguments
                        args = json.loads(raw_args) if raw_args and raw_args != "null" else {}
                    else:
                        fn   = tc.get("function", {})
                        name = fn.get("name", "")
                        args = fn.get("arguments", {})

                    # Emit a tool-request event for the frontend to show which tool was selected.
                    yield _sse("tool_requested", json.dumps({
                        "tool": name,
                        "args": args,
                    }))

                    if search_count >= max_searches:
                        yield _sse("status", f"Search cap ({max_searches}) reached, answering from context...")
                        tool_call_id = tc.id if CONFIG["use_groq"] else f"call_{name}"
                        conversation_history.append({
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "content": f"Search limit of {max_searches} reached."
                        })
                        continue

                    event_payload = {
                        "tool": name,
                        "args": args,
                    }
                    if name == "web_search":
                        event_payload["count"] = search_count + 1
                        event_payload["max"] = max_searches
                        event_payload["label"] = f'searching: "{args.get("query", "")}" ({search_count + 1}/{max_searches})'
                    elif name == "fetch_webpage":
                        event_payload["label"] = f'fetching: {args.get("url", "")}'
                    elif name == "list_files":
                        event_payload["label"] = "listing workspace files"                    
                    elif name == "read_file":
                        event_payload["label"] = f'reading: {args.get("path", "")}'
                    elif name == "calculate":
                        event_payload["label"] = f'calculating: {args.get("expression", "")}'

                    else:
                        event_payload["label"] = f'{name}: {json.dumps(args)}'

                    yield _sse("searching", json.dumps(event_payload))
                    
                    log.info("tool_call | %s | args=%s", name, json.dumps(args))

                    result = await mcp.call_tool(name, args)

                    # summarize search/fetch results before storing to save tokens
                    if CONFIG["use_groq"] and groq_client and name in ("web_search", "fetch_webpage"):
                        result = await _summarize_tool_result(groq_client, name, result)
                        yield _sse("status", f"summarized {name} result")

                    if name in ("web_search", "fetch_webpage"):
                        search_count += 1
                        
                    yield _sse("search_result", json.dumps({
                        "tool": name,
                        "count": search_count
                    }))
                    tool_call_id = tc.id if CONFIG["use_groq"] else f"call_{name}"
                    conversation_history.append({
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": result
                    })

                # Loop back → Ollama sees tool results, decides next step
    except Exception as e:
        yield _sse("error", traceback.format_exc())
        yield _sse("done", "{}")
    
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)