# local-search-agent

A local MCP-based agentic search tool. Gemma 4 (via Ollama) decides when
to search, calls a real MCP server for web search + page fetch, and streams
answers back to a browser UI.

```
[index.html]  →  HTTP/SSE  →  [server.py]  →  MCP stdio  →  [mcp_server.py]
  browser           API         FastAPI          protocol     tavily
                              + Ollama loop
```

## Setup

```bash
# 1. Install deps
pip install -r requirements.txt

# 2. Pull model
ollama pull gemma4:e2b

# 3. Start MCP + API server
python server.py
# uvicorn server:app --reload --port 8000

# 4. Open index.html in your browser (just open the file directly)
```

## Config

Everything is configurable from the sidebar in the UI at runtime.
You can also set env vars before starting the server:

| Env var        | Default                  | Description                    |
|----------------|--------------------------|--------------------------------|
| MODEL          | gemma4:2b                | Ollama model name              |
| MAX_SEARCHES   | 5                        | Max searches per turn          |
| TEMPERATURE    | 0.7                      | Generation temperature         |
| MAX_TOKENS     | 1024                     | Max tokens per response        |
| OLLAMA_URL     | http://localhost:11434   | Ollama base URL                |
| BRAVE_API_KEY  | (empty)                  | Optional Brave Search fallback |

## Resume blurb

> Built a local agentic search assistant using the Model Context Protocol (MCP).
> Implemented an MCP server (stdio transport, JSON-RPC 2.0) exposing web search
> and page extraction tools, with a FastAPI client running an Ollama agentic loop
> (Gemma 4 2B). Features a streaming browser UI with live search status and
> runtime config — fully offline, zero cloud APIs.
