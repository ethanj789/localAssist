import os
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

CONFIG = {
    "use_groq":         os.getenv("USE_GROQ", "false").lower() == "true",
    "groq_api_key":     os.getenv("GROQ_API_KEY", ""),
    "groq_model":       os.getenv("GROQ_MODEL", "llama-3.1-8b-instant"),
    "ollama_base_url":  os.getenv("OLLAMA_URL", "http://localhost:11434"),
    "model":            os.getenv("MODEL", "gemma4:e2b"),
    "max_searches":     int(os.getenv("MAX_SEARCHES", "2")),
    "max_tokens":       int(os.getenv("MAX_TOKENS", "1024")),
    "temperature":      float(os.getenv("TEMPERATURE", "0.7")),
    "mcp_server_cmd":   os.getenv("MCP_CMD", f"python mcp_server.py"),
    "system_prompt":    os.getenv("SYSTEM_PROMPT", (
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
# SUMMARY_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"  # always use small model for compression tasks
SUMMARY_MODEL = "llama-3.1-8b-instant"

# history options
MAX_HISTORY = 10
SUMMARIZE_THRESHOLD = 8
SUMMARIZE_KEEP_LAST = 3

# coding model routing
CODING_MODEL = "llama-3.3-70b-versatile"
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

# memory
MEMORY_FILE = Path(__file__).parent / "aiNotes/memory.json"

def load_memory() -> str:
    try:
        import json
        if MEMORY_FILE.exists():
            data = json.loads(MEMORY_FILE.read_text())
            if not data:
                return ""
            lines = [f"- {k}: {v}" for k, v in data.items()]
            return "User memory:\n" + "\n".join(lines)
    except Exception:
        pass
    return ""

def select_groq_model(message: str) -> tuple[str, str]:
    lower = message.lower()
    words = set(lower.split())
    is_coding = bool(words & CODING_KEYWORDS) or any(p in lower for p in CODING_PHRASES)
    tool_model = CONFIG["groq_model"]
    answer_model = CODING_MODEL if is_coding else CONFIG["groq_model"]
    return tool_model, answer_model