import json
import os
from pathlib import Path
import re
import sys
from typing import Any

from dotenv import load_dotenv
load_dotenv()

ROOT_DIR = Path(__file__).resolve().parent
MODEL_SETTINGS_PATH = ROOT_DIR / "model_settings.json"


def _load_model_settings() -> dict[str, dict[str, Any]]:
    defaults = {
        "default": {
            "provider": "ollama",
            "model": "gemma4:e2b",
            "temperature": 0.7,
            "max_tokens": 5000,
            "reasoning_effort": "",
            "thinking": False,
            "enabled": True,
        },
        "tool": {
            "provider": "groq",
            "model": "openai/gpt-oss-20b",
            "openrouter_model": "openai/gpt-oss-20b:free",
            "temperature": 0.7,
            "max_tokens": 5000,
            "reasoning_effort": "",
            "enabled": True,
        },
        "answer": {
            "provider": "groq",
            "model": "openai/gpt-oss-20b",
            "openrouter_model": "openai/gpt-oss-20b:free",
            "temperature": 0.7,
            "max_tokens": 5000,
            "reasoning_effort": "",
            "enabled": True,
        },
        "coding": {
            "provider": "groq",
            "model": "qwen/qwen3.6-27b",
            "openrouter_model": "qwen/qwen3-coder:free",
            "temperature": 0.7,
            "max_tokens": 5000,
            "reasoning_effort": "",
            "enabled": True,
        },
        "thinking": {
            "provider": "groq",
            "model": "openai/gpt-oss-120b",
            "openrouter_model": "openai/gpt-oss-120b:free",
            "temperature": 0.7,
            "max_tokens": 5000,
            "reasoning_effort": "",
            "enabled": True,
        },
        "summarize": {
            "provider": "groq",
            "model": "openai/gpt-oss-20b",
            "openrouter_model": "openai/gpt-oss-20b:free",
            "temperature": 0.3,
            "max_tokens": 300,
            "reasoning_effort": "",
            "enabled": True,
        },
        "compact": {
            "provider": "groq",
            "model": "openai/gpt-oss-20b",
            "openrouter_model": "openai/gpt-oss-20b:free",
            "temperature": 0.1,
            "max_tokens": 200,
            "reasoning_effort": "",
            "enabled": True,
        },
        "title": {
            "provider": "groq",
            "model": "openai/gpt-oss-20b",
            "openrouter_model": "openai/gpt-oss-20b:free",
            "temperature": 0.5,
            "max_tokens": 150,
            "reasoning_effort": "",
            "enabled": True,
        },
    }
    try:
        if MODEL_SETTINGS_PATH.exists():
            with MODEL_SETTINGS_PATH.open("r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                for key, value in defaults.items():
                    if isinstance(loaded.get(key), dict):
                        merged = dict(value)
                        merged.update({k: v for k, v in loaded[key].items() if isinstance(v, (str, int, float, bool)) or v is None})
                        defaults[key] = merged
    except Exception as exc:
        print(f"[config] failed to load model config {MODEL_SETTINGS_PATH}: {exc}")

    return defaults


MODEL_SETTINGS = _load_model_settings()


def get_model_setting(name: str, fallback: str | None = None) -> dict[str, Any]:
    entry = MODEL_SETTINGS.get(name) or {}
    if not entry:
        return {}
    if entry.get("enabled", True):
        return dict(entry)
    if fallback:
        fallback_entry = MODEL_SETTINGS.get(fallback) or {}
        if fallback_entry:
            return dict(fallback_entry)
    return dict(entry)


def get_model_value(name: str, key: str, fallback: Any = None) -> Any:
    return get_model_setting(name).get(key, fallback)


def _normalize_provider(value: Any) -> str | None:
    if isinstance(value, str):
        provider = value.strip().lower()
        if provider in {"groq", "openrouter", "ollama"}:
            return provider
    return None

configured_provider = os.getenv("USE_PROVIDER", "").strip().lower()
if configured_provider in {"groq", "openrouter", "ollama"}:
    use_external_provider_value = configured_provider in {"groq", "openrouter"}
else:
    use_external_provider_value = os.getenv("USE_EXTERNAL_PROVIDER", "false").lower() == "true"

# Cloud provider is always openrouter; groq remains available as an explicit override via USE_PROVIDER=groq
default_remote = "groq" if os.getenv("USE_PROVIDER", "").strip().lower() == "groq" else "openrouter"

CONFIG = {
    "open_router_override": os.getenv("USE_OPENROUTER_FIRST", "false").lower() == "true",
    "use_provider": configured_provider or (default_remote if use_external_provider_value else "ollama"),
    "use_external_provider": use_external_provider_value,
    "groq_model":       os.getenv("GROQ_MODEL", get_model_value("tool", "model", "openai/gpt-oss-20b")),
    "ollama_base_url":  os.getenv("OLLAMA_URL", "http://localhost:11434"),
    "model":            os.getenv("MODEL", get_model_value("default", "model", "gemma4:e2b")),
    "max_searches":     int(os.getenv("MAX_SEARCHES", "2")),
    "max_emails":       int(os.getenv("MAX_EMAILS", "1")),
    "max_tools":        int(os.getenv("MAX_TOOLS", "10")),
    "max_tokens":       int(os.getenv("MAX_TOKENS", str(get_model_value("default", "max_tokens", 5000)))),
    "temperature":      float(os.getenv("TEMPERATURE", str(get_model_value("default", "temperature", 0.7)))),
    "mcp_server_cmd": os.getenv("MCP_CMD", f"{sys.executable} mcp_server.py"),
    # Ollama thinking mode — set OLLAMA_THINKING=true to enable for all requests.
    # Can also be set per-request via request_kwargs["thinking"] = True/False.
    "ollama_thinking": os.getenv("OLLAMA_THINKING", "false").lower() == "true",
}
# SUMMARY_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"  # using small model for compression tasks
# SUMMARY_MODEL = "llama-3.1-8b-instant" #deprecated :C
# SMALL_MODEL = "llama-3.1-8b-instant" #deprecated :C
SUMMARY_MODEL = get_model_value("summarize", "model", get_model_value("default", "model", "openai/gpt-oss-20b"))
SMALL_MODEL = get_model_value("tool", "model", SUMMARY_MODEL)
# RESPONSE_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"  #deprecated
RESPONSE_MODEL = get_model_value("answer", "model", get_model_value("default", "model", "qwen/qwen3.6-27b"))
# CODING_MODEL = "llama-3.3-70b-versatile"
CODING_MODEL = get_model_value("coding", "model", RESPONSE_MODEL)
THINKING_MODEL = get_model_value("thinking", "model", RESPONSE_MODEL)

# history options
MAX_HISTORY = 10
SUMMARIZE_THRESHOLD = 8
SUMMARIZE_KEEP_LAST = 3

# coding model routing
# CODING_MODEL = "qwen/qwen3.6-27b"
CODING_PHRASES = [
    "write a", "write me", "generate a", "create a", "make a",
    "fix this", "debug this", "help me code", "how do i implement",
    "whats wrong with", "can you code", "show me how to",
]
CODING_KEYWORDS = {
    "debug", "refactor", "algorithm", "recursion", "compile",
    "stackoverflow", "syntax error", "runtime error", "null pointer",
    "segfault", "dockerfile", "kubernetes", "git", "npm", "pip",
    "import", "instanceof", "polymorphism", "inheritance", "<coding>", "<thinking>"
}

# memory
MEMORY_FILE = Path(__file__).parent / "aiWorkspace/aiNotes/memory.json"

def load_memory() -> str:
    try:
        import json
        if MEMORY_FILE.exists():
            data = json.loads(MEMORY_FILE.read_text())
            if not data:
                return ""
            
            output = []
            
            # Manual memories
            manual = data.get("manual", {})
            if manual:
                output.append("User context (manual):")
                for k, v in manual.items():
                    output.append(f"- {k}: {v}")
            
            # Agent-managed memories
            agent_managed = data.get("agent_managed", [])
            if agent_managed:
                if output: output.append("") # spacer
                output.append("Memories:")
                for slot in agent_managed:
                    output.append(f"- {slot['content']}")
            
            return "\n".join(output)
    except Exception:
        pass
    return ""

def get_raw_memory() -> dict:
    try:
        import json
        if MEMORY_FILE.exists():
            return json.loads(MEMORY_FILE.read_text())
    except Exception:
        pass
    return {"manual": {}, "agent_managed": []}

def save_memory(data: dict):
    import json
    MEMORY_FILE.write_text(json.dumps(data, indent=4))

