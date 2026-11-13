import asyncio
import datetime
import os
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

# if sys.platform == "win32":
#     asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import logging
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config import CONFIG, get_raw_memory, save_memory
import llm_provider
from mcp_infra import MCPClient, MCPAggregator
from mcp_infra import config as mcp_config
import agent
from agent import agent_loop
from tools.index import BUSY_LOCK_FILE, build_index
from skills import list_skills, get_skill_by_command, run_skill
import db

# Voice transcription
# DEAD CODE — Whisper replaced by browser Web Speech API (see static/js/api/webSpeechVoice.js)
# from faster_whisper import WhisperModel
import json
from logging.handlers import RotatingFileHandler
# Default to INFO so the log stays readable (DEBUG dumps per-request payload
# summaries and stream internals). Set LOG_LEVEL=DEBUG for deep troubleshooting.
_log_level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
# Rotating log so it self-caps instead of growing forever: once logs.txt hits
# ~2 MB it rolls over to logs.txt.1 (and .2), keeping only the most recent
# entries. No more manual cleanup. Tune via LOG_MAX_BYTES / LOG_BACKUPS.
_log_handler = RotatingFileHandler(
    "logs.txt",
    maxBytes=int(os.getenv("LOG_MAX_BYTES", str(2 * 1024 * 1024))),
    backupCount=int(os.getenv("LOG_BACKUPS", "2")),
    encoding="utf-8",
)
logging.basicConfig(
    level=_log_level,
    format="%(asctime)s [%(levelname)s] %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[_log_handler],
)
logging.getLogger("watchfiles").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


STARTUP_TOKEN = str(uuid.uuid4())
# The aggregator wraps the unchanged local MCPClient (hand-rolled stdio subprocess
# running tools.registry.app) PLUS any external MCP servers from mcp_servers.json.
# It exposes the same async list_tools()/call_tool() surface, so it's a drop-in for
# the old raw MCPClient everywhere downstream (agent_loop, resume, skills, warmup).
mcp = MCPAggregator(MCPClient(CONFIG["mcp_server_cmd"]))

# DEAD CODE — Whisper model no longer used, transcription handled by browser Web Speech API
# whisper_model = WhisperModel("tiny.en", device="cpu", compute_type="int8")
# TEMP_DIR = Path("./voiceChats")
# TEMP_DIR.mkdir(exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[db] Initializing database...")
    db.init_db()
    print("[db] Cleaning up old conversations...")
    db.cleanup_old_conversations()
    # Sweep stale weather/news cache entries so one-off agent news queries don't
    # pile up in events_cache.json across restarts.
    try:
        from tools import events
        pruned = events.prune_stale_cache()
        if pruned:
            print(f"[events] Pruned {pruned} stale cache entr{'y' if pruned == 1 else 'ies'}.")
    except Exception as exc:
        logging.warning(f"[events] Cache prune failed (non-fatal): {exc}")
    # Starts the local MCP client AND connects external MCP servers (startup and
    # POST /mcp/reload share the same refresh path inside the aggregator).
    await mcp.open()
    try:
        _ext = mcp.external_status()
        _ext_enabled = [s["name"] for s in _ext if s.get("enabled")]
        print(f"[server] MCP started | local + external({len(_ext_enabled)}): {_ext_enabled} | model={CONFIG['model']} | use_provider={CONFIG.get('use_provider')} | cloud model={CONFIG.get('groq_model')}")
    except Exception:
        print(f"[server] MCP started | model={CONFIG['model']} | use_provider={CONFIG.get('use_provider')} | cloud model={CONFIG.get('groq_model')}")

    # Kick off a full index build in the background so pre-existing workspace
    # files are indexed and manifest.json is written without blocking startup.
    async def _background_index():
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, build_index)
            print("[index] Startup index build complete.")
        except Exception as exc:
            logging.error(f"[index] Startup index build failed: {exc}")

        # After indexing, run Windows Ink recognition over any changed pages.
        try:
            from tool_apps.notes.api import backfill_ocr_for_existing_pages
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, backfill_ocr_for_existing_pages)
            print("[ink] Startup Ink recognition backfill complete.")
        except Exception as exc:
            logging.error(f"[ink] Startup Ink recognition backfill failed: {exc}")

    asyncio.get_event_loop().create_task(_background_index())

    # Warm up Ollama KV cache so the first real turn skips the expensive prefill
    # phase. This must produce a prompt prefix that is BYTE-IDENTICAL to what the
    # first real turn sends (system string + tool schemas), otherwise the cached
    # prefix won't match and nothing is saved. We therefore reuse the agent's own
    # prompt-assembly helpers and the real _build_ollama_payload path rather than
    # hand-rolling a second payload shape that would silently drift.
    async def _warmup_ollama_kv():
        import httpx
        try:
            mcp_tools = await mcp.list_tools()
            system = agent.build_system_prompt()  # no edit log — matches a fresh chat
            tools = agent.build_ollama_tools(mcp_tools)
            request_kwargs = {
                "model": CONFIG["model"],
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": "hi"},
                ],
                "tools": tools,
                "temperature": CONFIG["temperature"],
            }
            payload = llm_provider._build_ollama_payload(request_kwargs, CONFIG, stream=False)
            # Only prefill — generate a single token (decode is irrelevant here).
            payload["options"]["num_predict"] = 1
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(
                    f"{CONFIG['ollama_base_url']}/api/chat", json=payload
                )
                resp.raise_for_status()
                print(f"[warmup] Ollama KV cache primed (system + {len(tools)} tools).")
        except Exception as exc:
            logging.warning(f"[warmup] Ollama KV warm-up failed (non-fatal): {exc}")

    asyncio.get_event_loop().create_task(_warmup_ollama_kv())

    yield
    await mcp.aclose()


app = FastAPI(title="Local MCP Search Agent", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static", html=True), name="static")

import importlib.util
import json

# Auto-mount tool_apps
tool_apps_dir = Path(__file__).parent / "tool_apps"
if tool_apps_dir.exists():
    for manifest_file in tool_apps_dir.glob("*/manifest.json"):
        tool_dir = manifest_file.parent
        try:
            with open(manifest_file, "r", encoding="utf-8") as f:
                manifest = json.load(f)
            
            # Mount static directory
            static_dir = manifest.get("static_dir", ".")
            mount_path = manifest.get("mount_path", f"/tools/{tool_dir.name}")
            app.mount(mount_path, StaticFiles(directory=str(tool_dir / static_dir), html=True), name=f"{tool_dir.name}_static")
            
            # Include API router
            api_module_name = manifest.get("api_module")
            if api_module_name:
                api_prefix = manifest.get("api_prefix", f"/api/{tool_dir.name}")
                module_path = tool_dir / f"{api_module_name}.py"
                if module_path.exists():
                    spec = importlib.util.spec_from_file_location(f"tool_apps.{tool_dir.name}.{api_module_name}", str(module_path))
                    module = importlib.util.module_from_spec(spec)
                    sys.modules[spec.name] = module
                    spec.loader.exec_module(module)
                    if hasattr(module, "router"):
                        app.include_router(module.router, prefix=api_prefix, tags=[tool_dir.name])
        except Exception as e:
            logging.error(f"Failed to load tool app {tool_dir.name}: {e}")

@app.get("/")
def root():
    return FileResponse("static/index.html")


# ============ REINDEX ENDPOINTS ============

@app.post("/reindex/files")
async def reindex_files():
    """Force a full workspace file index rebuild in the background."""
    import shutil
    async def _run():
        try:
            await asyncio.get_running_loop().run_in_executor(None, lambda: build_index(force=True))
            logging.info("[reindex] File index rebuild complete.")
        except Exception as exc:
            logging.error(f"[reindex] File index rebuild failed: {exc}")
    asyncio.get_event_loop().create_task(_run())
    return {"status": "ok", "message": "File re-index started in background."}


@app.post("/reindex/ocr")
async def reindex_ocr():
    """Wipe existing recognized-text output and re-run Ink recognition for all pages."""
    import shutil
    from tool_apps.notes.api import backfill_ocr_for_existing_pages, OUTPUT_DIR
    # Clear existing output so backfill processes everything fresh
    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    async def _run():
        try:
            await asyncio.get_running_loop().run_in_executor(None, backfill_ocr_for_existing_pages)
            logging.info("[reindex] Ink recognition backfill complete.")
            # Recognized note text is indexed as ordinary files now — a normal
            # build_index picks up the regenerated 0.txt files (and runs the
            # notes-layout migration/cleanup) in one pass.
            from tools.index import build_index
            await asyncio.get_running_loop().run_in_executor(None, build_index)
            logging.info("[reindex] Workspace reindex complete.")
        except Exception as exc:
            logging.error(f"[reindex] Ink re-index failed: {exc}")
    asyncio.get_event_loop().create_task(_run())
    return {"status": "ok", "message": "Ink recognition re-index started in background."}

class ChatRequest(BaseModel):
    message: str
    model: str = "default"
    conversation_id: str | None = None
    ollama_thinking: bool = False
    # List of attachments the user added (images, PDFs, and future doc types —
    # the whole attachment bucket, not image-only). Each item is either a bare
    # `data:<mime>;base64,...` URL or a `{name, dataUrl}` object carrying the
    # original filename. The agent runs each through prep_file (render pages +
    # extract text), persists them under chat_files/, and feeds page images on
    # the sending turn.
    attachments: list[str | dict] = []


# DEAD CODE — VoiceChatResponse no longer used (browser Web Speech API handles transcription)
# class VoiceChatResponse(BaseModel):
#     transcription: str
#     message: str  # This will be the chat response


# ============ ENDPOINTS ============

@app.get("/config")
async def get_config():
    return {k: v for k, v in CONFIG.items() if k != "mcp_server_cmd"}


@app.post("/config")
async def update_config(updates: dict):
    allowed = {"model", "max_searches", "max_tokens", "temperature", "use_provider", "use_external_provider"}
    for k, v in updates.items():
        if k not in allowed:
            continue
        if k == "use_provider":
            provider = str(v).strip().lower() if isinstance(v, str) else ""
            CONFIG["use_provider"] = provider if provider in {"groq", "openrouter", "ollama"} else "ollama"
            CONFIG["use_external_provider"] = CONFIG["use_provider"] in {"groq", "openrouter"}
        else:
            CONFIG[k] = v
    return {"status": "ok", "config": {k: CONFIG[k] for k in ("model", "max_searches", "max_tokens", "temperature", "use_provider", "use_external_provider") if k in CONFIG}}


@app.get("/mcp/servers")
async def get_mcp_servers():
    """Sanitized external MCP server list for the UI (no secret headers/env).
    Reflects the current config + sidecar, so disabled/failed servers appear
    too. The in-memory tool list is authoritative; this is config/status view."""
    return {"servers": mcp.external_status()}


@app.post("/mcp/reload")
async def reload_mcp_servers():
    """Re-read mcp_servers.json / mcp_meta.json and rebuild external servers
    (same refresh path used at startup). New/removed tools take effect on the
    NEXT turn for all conversations (KV-cache reset accepted). Local tools are
    unaffected."""
    await mcp.refresh()
    return {"status": "ok", "servers": mcp.external_status()}


# ── MCP config writes (add / delete / enable-disable), each auto-reloads ──────
#
# These mutate mcp_servers.json / mcp_meta.json then rebuild external servers so
# the change is live without a restart. Secrets (env/headers) are accepted on
# add but never echoed back — the response is the same sanitized list the read
# endpoint returns. Add/delete edit the standard config; toggle only flips the
# sidecar's `enabled`.

class McpServerIn(BaseModel):
    name: str
    # A standard mcpServers block: stdio (command/args/env) or remote (url/
    # headers, optional type:"sse"). Validated + canonicalized server-side.
    command: str | None = None
    args: list[str] = []
    env: dict[str, str] = {}
    url: str | None = None
    headers: dict[str, str] = {}
    transport: str | None = None  # only "sse" is meaningful (http is default)
    overwrite: bool = False


class McpToggleIn(BaseModel):
    name: str
    enabled: bool


@app.post("/mcp/servers")
async def add_mcp_server(server: McpServerIn):
    """Add a server to mcp_servers.json, then reload. Returns the fresh
    sanitized list so the UI re-renders in one round-trip."""
    block: dict = {}
    if server.command:
        block["command"] = server.command
        if server.args:
            block["args"] = server.args
        if server.env:
            block["env"] = server.env
    elif server.url:
        block["url"] = server.url
        if (server.transport or "").strip().lower() == "sse":
            block["type"] = "sse"
        if server.headers:
            block["headers"] = server.headers
    try:
        mcp_config.add_server(server.name, block, overwrite=server.overwrite)
    except mcp_config.ConfigWriteError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await mcp.refresh()
    return {"status": "ok", "servers": mcp.external_status()}


@app.delete("/mcp/servers/{name}")
async def delete_mcp_server(name: str):
    """Remove a server from mcp_servers.json (and prune its sidecar), then
    reload."""
    try:
        mcp_config.remove_server(name)
    except mcp_config.ConfigWriteError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    await mcp.refresh()
    return {"status": "ok", "servers": mcp.external_status()}


@app.post("/mcp/servers/toggle")
async def toggle_mcp_server(body: McpToggleIn):
    """Enable/disable a server (sidecar only — config untouched), then reload so
    a disabled server drops its tools and an enabled one connects."""
    try:
        mcp_config.set_enabled(body.name, body.enabled)
    except mcp_config.ConfigWriteError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    await mcp.refresh()
    return {"status": "ok", "servers": mcp.external_status()}


@app.get("/memory")
async def get_memory():
    return get_raw_memory()


class MemorySlotIn(BaseModel):
    key: str
    value: str
    bucket: str = "agent_managed"  # "manual" | "agent_managed" (UI always sends explicit)


def _normalize_mem_key(key: str) -> str:
    # Mirror tools.memory._normalize_key so UI-written keys collapse the same way
    # (case/spacing) as agent-written ones.
    return "_".join(key.strip().lower().split())


@app.put("/memory")
async def upsert_memory_slot(slot: MemorySlotIn):
    bucket = slot.bucket
    if bucket not in ("manual", "agent_managed"):
        raise HTTPException(status_code=400, detail=f"Invalid bucket '{bucket}'.")

    k = _normalize_mem_key(slot.key)
    value = (slot.value or "").strip()
    if not k:
        raise HTTPException(status_code=400, detail="Key is empty after normalization.")
    if not value:
        raise HTTPException(status_code=400, detail="Value is required.")

    data = get_raw_memory()
    manual = data.get("manual") if isinstance(data.get("manual"), dict) else {}
    agent_managed = data.get("agent_managed") if isinstance(data.get("agent_managed"), dict) else {}

    if bucket == "manual":
        # A key lives in exactly one bucket: promoting to manual drops any
        # agent-managed entry with the same key.
        agent_managed.pop(k, None)
        manual[k] = value  # manual entries are plain key -> value strings
    else:
        manual.pop(k, None)
        existed = k in agent_managed
        created = agent_managed.get(k, {}).get("created") if existed else None
        now = datetime.datetime.now().isoformat()
        agent_managed[k] = {
            "value": value,
            "created": created or now,
            "updated": now,
        }

    data["manual"] = manual
    data["agent_managed"] = agent_managed
    save_memory(data)
    return {"status": "ok", "bucket": bucket, "key": k}


@app.delete("/memory/{bucket}/{key}")
async def delete_memory_slot(bucket: str, key: str):
    if bucket not in ("manual", "agent_managed"):
        raise HTTPException(status_code=400, detail=f"Invalid bucket '{bucket}'.")

    data = get_raw_memory()
    section = data.get(bucket)
    if not isinstance(section, dict):
        section = {}
    if key not in section:
        raise HTTPException(status_code=404, detail=f"'{key}' not found in {bucket}.")
    del section[key]
    data[bucket] = section
    save_memory(data)
    return {"status": "ok"}

# ============ EDIT CONFIRMATION (break-and-resume) ============
#
# The old decoupled model (propose → separate /resolve_edit write + placeholder
# rewrite, with in-memory global_pending_edits) is gone. Pending edits are now
# DB truth: a conversation is "pending" iff its last assistant tool_calls
# message has an unanswered confirm-required call (db.get_pending_tool_call).
#
# Contract endpoints: GET /pending_edit/{conversation_id}, POST /respond.
# /chat has a 409 backstop. The old /resolve_edit and /pending_edits endpoints
# have been removed — the frontend uses the break-and-resume contract.


def _pending_edit_payload(conversation_id: str) -> dict | None:
    """Build the GET /pending_edit contract payload for a conversation, or None
    if nothing is pending. Recomputes the preview against the CURRENT file."""
    pending = db.get_pending_tool_call(conversation_id)
    if not pending:
        return None

    raw_args = pending.get("arguments") or "{}"
    if isinstance(raw_args, str):
        try:
            edit_args = json.loads(raw_args) if raw_args and raw_args != "null" else {}
        except json.JSONDecodeError:
            edit_args = {}
    else:
        edit_args = raw_args or {}
    # Sanitize invalid JSON escapes so the preview rendered on chat open/reload
    # matches the byte-correct content that will be written on approve.
    from tools.edit import strip_invalid_json_escapes
    edit_args = strip_invalid_json_escapes(edit_args)

    edit_inputs = {
        "path": edit_args.get("path", ""),
        "action": edit_args.get("action", "replace"),
        "anchor": edit_args.get("anchor", ""),
        "content": edit_args.get("content", ""),
        "replace_all": bool(edit_args.get("replace_all", False)),
        "summary": edit_args.get("summary", ""),
    }
    preview = agent._resolve_preview(edit_inputs)
    return {
        "toolCallId": pending["tool_call_id"],
        "conversation_id": conversation_id,
        "tool": pending["name"],
        "edit": {
            "path": edit_inputs["path"],
            "action": edit_inputs["action"],
            "anchor": edit_inputs["anchor"],
            "replace_all": edit_inputs["replace_all"],
            "summary": edit_inputs["summary"],
        },
        "preview": preview,
    }


@app.get("/pending_edit/{conversation_id}")
async def get_pending_edit(conversation_id: str):
    """Return the conversation's current pending edit (for rendering the modal
    on chat open / reload), recomputing the preview against the CURRENT file, or
    `null` (HTTP 200) when there is no pending edit."""
    return _pending_edit_payload(conversation_id)


class RespondRequest(BaseModel):
    conversation_id: str
    toolCallId: str
    decision: str  # "approve" | "reject"
    reason: str | None = None  # optional user-supplied rejection reason


@app.post("/respond")
async def respond(req: RespondRequest):
    """Resolve a pending edit and resume the agent. Returns a streaming SSE
    response (same shape as /chat) — the resumed turn streams back here."""
    pending = db.get_pending_tool_call(req.conversation_id)
    if not pending or pending["tool_call_id"] != req.toolCallId:
        raise HTTPException(status_code=409, detail="No matching pending edit.")

    return StreamingResponse(
        agent.resume_after_confirmation(req.conversation_id, req.toolCallId, req.decision, mcp, reason=req.reason),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/skills")
async def get_skills():
    """Return available skills for the frontend autocomplete."""
    return list_skills()


# ============ DASHBOARD (empty-chat home panel) ============

@app.get("/api/dashboard")
async def get_dashboard(city: str = "", news: str = "general"):
    """Serve the empty-chat dashboard data (weather + news).

    Both come from the persistent `events_cache.json` cache in tools.events:
    weather refetches at most once per 15 min per city, news at most once per
    hour per query. A stale entry is refreshed lazily on this call (not on a
    timer), so the limited GNews budget is only spent when a dashboard is
    actually viewed. `city` empty => wttr.in geolocates by IP.
    """
    from tools import events

    async def _weather():
        try:
            return await events.get_weather_dashboard(city)
        except Exception as exc:
            logging.warning("[dashboard] weather failed: %s", exc)
            return None

    async def _news():
        try:
            return await events.get_news_dashboard(news)
        except Exception as exc:
            logging.warning("[dashboard] news failed: %s", exc)
            return []

    weather, news_items = await asyncio.gather(_weather(), _news())
    return {
        "weather": weather,
        "news": news_items,
        "fetched_at": datetime.datetime.now().isoformat(),
    }


@app.post("/chat")
async def chat(req: ChatRequest):
    current_provider = llm_provider.get_effective_provider(CONFIG)
    conv_id = req.conversation_id
    if not conv_id:
        conv_id = db.create_conversation(provider=current_provider)
    else:
        # Backstop: refuse a new message while a confirm-required edit is still
        # pending for this conversation. The FE should already disable input;
        # this is the safety net. (A fresh conv_id can't be pending.)
        if db.get_pending_tool_call(conv_id):
            raise HTTPException(status_code=409, detail="Resolve the pending edit before sending a new message.")

    # Detect slash commands → route to skill executor
    message = req.message.strip()
    if message.startswith("/"):
        parts = message.split(None, 1)
        command = parts[0].lower()
        raw_args = parts[1] if len(parts) > 1 else ""

        skill = get_skill_by_command(command)
        if skill:
            async def skill_stream():
                # Emit conversation_id first so frontend can track it
                yield f"data: {json.dumps({'event': 'conversation_id', 'data': conv_id})}\n\n"
                async for chunk in run_skill(skill, raw_args, mcp, conv_id):
                    yield chunk

            return StreamingResponse(
                skill_stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

    # Normal chat flow
    return StreamingResponse(
        agent_loop(req.message, mcp, model_override=req.model, conversation_id=conv_id, ollama_thinking=req.ollama_thinking, attachments=req.attachments),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/chat-file/{rel_path:path}")
async def get_chat_file(rel_path: str):
    """Serve a stored chat file (rendered page image, original, or text) by its
    relative path, used to restore attachments when reopening a conversation.
    Path traversal is blocked in file_store._resolve."""
    import file_store
    data = file_store.load_bytes(rel_path)
    if data is None:
        raise HTTPException(status_code=404, detail="File not found")
    from fastapi.responses import Response
    return Response(content=data, media_type=file_store.guess_mime(rel_path))


class RenameConversationRequest(BaseModel):
    title: str


@app.get("/conversations")
async def get_conversations():
    db.cleanup_old_conversations()
    return db.get_conversations()


@app.get("/conversations/{conversation_id}")
async def get_conversation_details(conversation_id: str):
    conv = db.get_conversation(conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    messages = db.get_messages(conversation_id)
    return {"conversation": conv, "messages": messages}


@app.get("/subagent_runs/{conversation_id}")
async def get_subagent_runs(conversation_id: str):
    """Return all persisted subagent runs for a conversation (oldest first),
    each with its parsed transcript, so the subagent-activity panel can restore
    past runs on reopen. Returns an empty list when there are none."""
    return {"runs": db.get_subagent_runs(conversation_id)}


@app.put("/conversations/{conversation_id}")
async def rename_conversation(conversation_id: str, req: RenameConversationRequest):
    conv = db.get_conversation(conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    db.update_conversation_title(conversation_id, req.title)
    return {"status": "ok", "title": req.title}


@app.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str):
    conv = db.get_conversation(conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    db.delete_conversation(conversation_id)
    return {"status": "ok"}



@app.get("/startup-token")
async def startup_token():
    return {"token": STARTUP_TOKEN}


# @app.get("/status")
# async def get_status():
#     is_busy = BUSY_LOCK_FILE.exists()
#     return {"busy": is_busy}

import asyncio
from fastapi.responses import StreamingResponse

@app.get("/status/stream")
async def status_stream():
    async def event_generator():
        last_state = None
        while True:
            is_busy = BUSY_LOCK_FILE.exists()
            if is_busy != last_state:          # only push on change
                last_state = is_busy
                yield f"data: {json.dumps({'event': 'status', 'data': is_busy})}\n\n"
            await asyncio.sleep(1)             # check file every 1s (server-side only)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",         # important if behind nginx
        }
    )


# ============ VOICE ENDPOINTS (DEAD CODE — replaced by browser Web Speech API) ============

# @app.post("/transcribe")
# async def transcribe(audio: UploadFile = File(...)):
#     """
#     Transcribe audio file to text only.
#
#     Usage: Send audio file, get back transcribed text.
#     Then call /chat with that text if you want a response.
#     """
#     try:
#         # Save audio file temporarily
#         audio_path = TEMP_DIR / audio.filename
#         with open(audio_path, "wb") as f:
#             f.write(await audio.read())
#
#         # Transcribe using Whisper (local, free)
#         segments, _ = whisper_model.transcribe(str(audio_path))
#         text = " ".join([s.text for s in segments]).strip()
#
#         # Cleanup
#         audio_path.unlink()
#
#         if not text:
#             raise HTTPException(status_code=400, detail="Could not transcribe audio")
#
#         return {"text": text}
#
#     except HTTPException:
#         raise
#     except Exception as e:
#         logging.error(f"Transcription error: {e}")
#         raise HTTPException(status_code=500, detail=f"Transcription failed: {str(e)}")


# @app.post("/voice-chat")
# async def voice_chat(audio: UploadFile = File(...)):
#     """
#     All-in-one endpoint: audio → transcribe → chat response
#
#     Streams SSE events:
#     1. transcription event with what was said
#     2. Then chat response tokens
#     """
#     try:
#         # Step 1: Save and transcribe
#         audio_path = TEMP_DIR / audio.filename
#         with open(audio_path, "wb") as f:
#             f.write(await audio.read())
#
#         segments, _ = whisper_model.transcribe(str(audio_path))
#         transcription = " ".join([s.text for s in segments]).strip()
#
#         audio_path.unlink()
#
#         if not transcription:
#             raise HTTPException(status_code=400, detail="Could not transcribe audio")
#
#         # Step 2: Stream the chat response with transcription first
#         async def response_stream():
#             import json
#             # Send transcription as SSE event first
#             yield f"data: {json.dumps({'event': 'transcription', 'data': transcription})}\n\n"
#             # Then stream the chat response
#             async for chunk in agent_loop(transcription, mcp):
#                 yield chunk
#
#         return StreamingResponse(
#             response_stream(),
#             media_type="text/event-stream",
#             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
#         )
#
#     except HTTPException:
#         raise
#     except Exception as e:
#         logging.error(f"Voice chat error: {e}")
#         raise HTTPException(status_code=500, detail=f"Voice chat failed: {str(e)}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True,         reload_excludes=[
            "aiWorkspace/*",     # Matches 'aiWorkspace/simpleHelper.py' 
            "aiWorkspace/*/*",    # Matches files inside nested subfolders if any
            "**/logs.txt",
        ])
