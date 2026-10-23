import asyncio
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
from mcp_client import MCPClient
import agent
from agent import agent_loop
from tools.index import BUSY_LOCK_FILE, build_index
from skills import list_skills, get_skill_by_command, run_skill
import db

# Voice transcription
# DEAD CODE — Whisper replaced by browser Web Speech API (see static/js/api/webSpeechVoice.js)
# from faster_whisper import WhisperModel
import json
logging.basicConfig(
    filename="logs.txt",
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logging.getLogger("watchfiles").setLevel(logging.WARNING)


STARTUP_TOKEN = str(uuid.uuid4())
mcp = MCPClient(CONFIG["mcp_server_cmd"])

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
    print("[server] Loading Whisper model...")
    # Whisper no longer loaded — dead code
    await asyncio.get_event_loop().run_in_executor(None, mcp.start)
    print(f"[server] MCP client started | model={CONFIG['model']} | use_provider={CONFIG.get('use_provider')} | cloud model={CONFIG.get('groq_model')}")

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
    await asyncio.get_event_loop().run_in_executor(None, mcp.stop)


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
            # Re-run note blob scan to update the vector DB
            from tools.index import scan_note_blobs
            import tools.vector_db as vector_db
            from tools.index import DB_FILE
            vector_db.init_vector_db(DB_FILE)
            await asyncio.get_running_loop().run_in_executor(None, scan_note_blobs)
            logging.info("[reindex] Note blob scan complete.")
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
    exposed = {k: v for k, v in CONFIG.items() if k != "mcp_server_cmd"}
    # Expose groq_model under the friendlier cloud_model key for the frontend
    exposed["cloud_model"] = exposed.get("groq_model", "")
    return exposed


@app.post("/config")
async def update_config(updates: dict):
    allowed = {"model", "max_searches", "max_tokens", "temperature", "use_provider", "use_external_provider", "cloud_model"}
    for k, v in updates.items():
        if k not in allowed:
            continue
        if k == "use_provider":
            provider = str(v).strip().lower() if isinstance(v, str) else ""
            CONFIG["use_provider"] = provider if provider in {"groq", "openrouter", "ollama"} else "ollama"
            CONFIG["use_external_provider"] = CONFIG["use_provider"] in {"groq", "openrouter"}
        elif k == "cloud_model":
            # Frontend sends cloud_model; store it under the internal groq_model key
            CONFIG["groq_model"] = v
        else:
            CONFIG[k] = v
    return {"status": "ok", "config": {k: CONFIG[k] for k in ("model", "max_searches", "max_tokens", "temperature", "use_provider", "use_external_provider", "groq_model") if k in CONFIG}}


@app.delete("/history")
async def clear_history():
    # db.clear_all_conversations()
    # Pending edits are now DB truth (no in-memory globals to clear). Clearing
    # chat history would remove any dangling confirm tool calls along with it.
    return {"status": "cleared"}



@app.get("/memory")
async def get_memory():
    return get_raw_memory()


@app.delete("/memory/{index}")
async def delete_memory_slot(index: int):
    data = get_raw_memory()
    agent_managed = data.get("agent_managed", [])
    new_list = [s for s in agent_managed if s["index"] != index]
    if len(new_list) == len(agent_managed):
        raise HTTPException(status_code=404, detail=f"Slot {index} not found")
    data["agent_managed"] = new_list
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

    edit_inputs = {
        "path": edit_args.get("path", ""),
        "action": edit_args.get("action", "replace"),
        "anchor": edit_args.get("anchor", ""),
        "content": edit_args.get("content", ""),
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


@app.post("/respond")
async def respond(req: RespondRequest):
    """Resolve a pending edit and resume the agent. Returns a streaming SSE
    response (same shape as /chat) — the resumed turn streams back here."""
    pending = db.get_pending_tool_call(req.conversation_id)
    if not pending or pending["tool_call_id"] != req.toolCallId:
        raise HTTPException(status_code=409, detail="No matching pending edit.")

    return StreamingResponse(
        agent.resume_after_confirmation(req.conversation_id, req.toolCallId, req.decision, mcp),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/skills")
async def get_skills():
    """Return available skills for the frontend autocomplete."""
    return list_skills()


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
