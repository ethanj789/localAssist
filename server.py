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
import db

# Voice transcription
from faster_whisper import WhisperModel
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

# Load Whisper model (tiny.en = ~250MB, CPU-friendly)
whisper_model = WhisperModel("tiny.en", device="cpu", compute_type="int8")
TEMP_DIR = Path("./voiceChats")
TEMP_DIR.mkdir(exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[db] Initializing database...")
    db.init_db()
    print("[db] Cleaning up old conversations...")
    db.cleanup_old_conversations()
    print("[server] Loading Whisper model...")
    # Whisper already loaded above
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

        # After indexing, backfill OCR for any pages not yet processed.
        try:
            from tool_apps.notes.api import backfill_ocr_for_existing_pages
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, backfill_ocr_for_existing_pages)
            print("[ocr] Startup OCR backfill complete.")
        except Exception as exc:
            logging.error(f"[ocr] Startup OCR backfill failed: {exc}")

    asyncio.get_event_loop().create_task(_background_index())

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

class ChatRequest(BaseModel):
    message: str
    model: str = "default"
    conversation_id: str | None = None
    ollama_thinking: bool = False


class VoiceChatResponse(BaseModel):
    transcription: str
    message: str  # This will be the chat response


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
    agent.global_edit_log.clear()
    agent.global_pending_edit = None
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

class ResolveEditRequest(BaseModel):
    toolCallId: str
    action: str  # "approve" | "reject"
    reason: str = ""

@app.post("/resolve_edit")
async def resolve_edit(req: ResolveEditRequest):
    if not agent.global_pending_edit or agent.global_pending_edit["toolCallId"] != req.toolCallId:
        raise HTTPException(status_code=400, detail="No matching pending edit.")

    pending = agent.global_pending_edit
    conv_id = pending.get("conversation_id")
    if not conv_id:
        raise HTTPException(status_code=400, detail="Pending edit does not have a conversation ID.")

    # Mutate history in DB
    if req.action == "approve":
        content = "APPROVED"
        # Write to disk
        target = Path(pending["target_path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as f:
            f.write(pending["new_content"])
        agent.global_edit_log.append(f"{pending['path']} — {pending['summary']} — APPROVED")
    else:
        reason_str = req.reason.strip() if req.reason else "No reason provided"
        content = f"REJECTED: {reason_str}"
        agent.global_edit_log.append(f"{pending['path']} — {pending['summary']} — REJECTED")

    db.update_message_content(conv_id, req.toolCallId, content)
    agent.global_pending_edit = None
    return {"status": "ok"}


@app.post("/chat")
async def chat(req: ChatRequest):
    current_provider = llm_provider.get_effective_provider(CONFIG)
    conv_id = req.conversation_id
    if not conv_id:
        conv_id = db.create_conversation(provider=current_provider)
    return StreamingResponse(
        agent_loop(req.message, mcp, model_override=req.model, conversation_id=conv_id, ollama_thinking=req.ollama_thinking),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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


# ============ VOICE ENDPOINTS ============

@app.post("/transcribe")
async def transcribe(audio: UploadFile = File(...)):
    """
    Transcribe audio file to text only.
    
    Usage: Send audio file, get back transcribed text.
    Then call /chat with that text if you want a response.
    """
    try:
        # Save audio file temporarily
        audio_path = TEMP_DIR / audio.filename
        with open(audio_path, "wb") as f:
            f.write(await audio.read())
        
        # Transcribe using Whisper (local, free)
        segments, _ = whisper_model.transcribe(str(audio_path))
        text = " ".join([s.text for s in segments]).strip()
        
        # Cleanup
        audio_path.unlink()
        
        if not text:
            raise HTTPException(status_code=400, detail="Could not transcribe audio")
        
        return {"text": text}
        
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Transcription error: {e}")
        raise HTTPException(status_code=500, detail=f"Transcription failed: {str(e)}")


@app.post("/voice-chat")
async def voice_chat(audio: UploadFile = File(...)):
    """
    All-in-one endpoint: audio → transcribe → chat response
    
    Streams SSE events:
    1. transcription event with what was said
    2. Then chat response tokens
    """
    try:
        # Step 1: Save and transcribe
        audio_path = TEMP_DIR / audio.filename
        with open(audio_path, "wb") as f:
            f.write(await audio.read())
        
        segments, _ = whisper_model.transcribe(str(audio_path))
        transcription = " ".join([s.text for s in segments]).strip()
        
        audio_path.unlink()
        
        if not transcription:
            raise HTTPException(status_code=400, detail="Could not transcribe audio")
        
        # Step 2: Stream the chat response with transcription first
        async def response_stream():
            import json
            # Send transcription as SSE event first
            yield f"data: {json.dumps({'event': 'transcription', 'data': transcription})}\n\n"
            # Then stream the chat response
            async for chunk in agent_loop(transcription, mcp):
                yield chunk
        
        return StreamingResponse(
            response_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Voice chat error: {e}")
        raise HTTPException(status_code=500, detail=f"Voice chat failed: {str(e)}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True,         reload_excludes=[
            "aiWorkspace/*",     # Matches 'aiWorkspace/simpleHelper.py' 
            "aiWorkspace/*/*",    # Matches files inside nested subfolders if any
            "**/logs.txt",
        ])
