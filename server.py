import asyncio
import sys
import uuid
from contextlib import asynccontextmanager

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from config import CONFIG
from mcp_client import MCPClient
from agent import agent_loop, conversation_history

logging.basicConfig(
    filename="logs.txt",
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

STARTUP_TOKEN = str(uuid.uuid4())
mcp = MCPClient(CONFIG["mcp_server_cmd"])


@asynccontextmanager
async def lifespan(app: FastAPI):
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)