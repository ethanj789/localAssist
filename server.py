import asyncio
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
import logging
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from config import CONFIG
from mcp_client import MCPClient
from agent import agent_loop, conversation_history

# Voice transcription
from faster_whisper import WhisperModel

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


class ChatRequest(BaseModel):
    message: str


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
    conversation_history.clear()
    return {"status": "cleared"}


@app.post("/chat")
async def chat(req: ChatRequest):
    return StreamingResponse(
        agent_loop(req.message, mcp),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/startup-token")
async def startup_token():
    return {"token": STARTUP_TOKEN}


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