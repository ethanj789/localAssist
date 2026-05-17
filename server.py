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
from mcp_client import MCPClient
import agent
from agent import agent_loop
from tools.index import BUSY_LOCK_FILE

# Voice transcription
from faster_whisper import WhisperModel
import json
logging.basicConfig(
    filename="logs.txt",
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

STARTUP_TOKEN = str(uuid.uuid4())
mcp = MCPClient(CONFIG["mcp_server_cmd"])

# Load Whisper model (tiny.en = ~250MB, CPU-friendly)
whisper_model = WhisperModel("tiny.en", device="cpu", compute_type="int8")
TEMP_DIR = Path("./voiceChats")
TEMP_DIR.mkdir(exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[server] Loading Whisper model...")
    # Whisper already loaded above
    await asyncio.get_event_loop().run_in_executor(None, mcp.start)
    print(f"[server] MCP client started | model={CONFIG['model']} | use_groq={CONFIG['use_groq']} | groq model={CONFIG["groq_model"]}")
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
@app.get("/")
def root():
    return FileResponse("static/index.html")

class ChatRequest(BaseModel):
    message: str
    model: str = "default"


class VoiceChatResponse(BaseModel):
    transcription: str
    message: str  # This will be the chat response


# ============ ENDPOINTS ============

@app.get("/config")
async def get_config():
    return {k: v for k, v in CONFIG.items() if k != "mcp_server_cmd"}


@app.post("/config")
async def update_config(updates: dict):
    allowed = {"model", "max_searches", "max_tokens", "temperature", "system_prompt", "use_groq", "groq_model"}
    for k, v in updates.items():
        if k in allowed:
            CONFIG[k] = v
    return {"status": "ok", "config": {k: CONFIG[k] for k in allowed}}


@app.delete("/history")
async def clear_history():
    agent.conversation_history.clear()
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
    
    # Mutate history
    found = False
    for msg in agent.conversation_history:
        if msg.get("role") == "tool" and msg.get("tool_call_id") == req.toolCallId:
            if req.action == "approve":
                msg["content"] = "APPROVED"
                # Write to disk
                target = Path(pending["target_path"])
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("w", encoding="utf-8") as f:
                    f.write(pending["new_content"])
                agent.global_edit_log.append(f"{pending['path']} — {pending['summary']} — APPROVED")
            else:
                reason_str = req.reason.strip() if req.reason else "No reason provided"
                msg["content"] = f"REJECTED: {reason_str}"
                agent.global_edit_log.append(f"{pending['path']} — {pending['summary']} — REJECTED")
            found = True
            break
            
    if not found:
        raise HTTPException(status_code=404, detail="Tool call not found in history.")

    agent.global_pending_edit = None
    return {"status": "ok"}


@app.post("/chat")
async def chat(req: ChatRequest):
    return StreamingResponse(
        agent_loop(req.message, mcp, model_override=req.model),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)