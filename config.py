import os
from pathlib import Path
import re

from dotenv import load_dotenv
load_dotenv()

CONFIG = {
    "use_groq":         os.getenv("USE_GROQ", "false").lower() == "true",
    # "groq_api_key":     os.getenv("GROQ_API_KEY", ""),
    "groq_model":       os.getenv("GROQ_MODEL", "llama-3.1-8b-instant"),
    "ollama_base_url":  os.getenv("OLLAMA_URL", "http://localhost:11434"),
    "model":            os.getenv("MODEL", "gemma4:e2b"),
    "max_searches":     int(os.getenv("MAX_SEARCHES", "2")),
    "max_emails":       int(os.getenv("MAX_EMAILS", "1")),
    "max_tools":        int(os.getenv("MAX_TOOLS", "10")),
    "max_tokens":       int(os.getenv("MAX_TOKENS", "5000")),
    "temperature":      float(os.getenv("TEMPERATURE", "0.7")),
    "mcp_server_cmd":   os.getenv("MCP_CMD", f"python mcp_server.py"),
    "system_prompt":    os.getenv("SYSTEM_PROMPT", (
        "You are a helpful personal assistant. Communicate clearly with sufficient detail without filler. "

        "Tool usage rules: "
        "Only call a tool when the request clearly requires external information, real-time data, or performing an action like drafting an email. "
        "If unsure, do NOT call any tool. "

        "Tool priority order: "
        "1. recent_events (ONLY for news or weather) "
        "2. web_search (ONLY for non-news, non-weather external information such as niche topics or general web lookup) Do not use this for the weather."
        "Never violate tool boundaries."

        "recent_events is the ONLY allowed tool for news or weather queries. "
        "Never use web_search for news or weather under any circumstance. "
        "It is prefered when providing news or headline updates that the majority of all the headline results are shown to the user, as long as they are not duplicates. "
        "When summarizing the headlines, provide summaries and then links to the articles at the end of your response. Format the links with bullet points. "

        "Do not treat web_search as a fallback for news or weather information. "
        "If recent_events is used for a topic, prefer reusing it for follow-up questions about the same topic. "

        "For casual conversation, stable concepts, or anything you can answer confidently, respond directly without using tools. "
        "Do not use tools to confirm known information. "

        "When using web_search, do not stop at snippets if deeper detail is needed. "
        "Use fetch_webpage on the most relevant result for fuller context. Chain searches only when necessary. "

        "Always report what you found and note any gaps or uncertainty. Mention sources when possible. "

        "For work requiring math, make sure to use the calculate tool. This will ensure correct values are used in your response. "

        "You have read-only access to a file workspace. "
        "If the user asks to review, debug, or improve code, call list_files first, then read_file as needed before responding. "
        "If the user asks to generate or explain code from scratch, do it directly without accessing the workspace. "
        "Never write, modify, or delete files — only read and suggest changes in chat. "

        "For email drafting, once you have successfully called the draft_email tool, do not call it again for the same email. "
        "The tool opens a composer window for the user; your task for that specific email is complete once the tool returns success. "
        "When you decide to answer the user, do not call any tools. "
        "Only return a final message."
    )),
    "memory_management_prompt": (
        "You have 10 memory slots. Write memories that are long-term useful: "
        "user preferences, project facts, recurring corrections. Do NOT memorize "
        "things already in the system prompt or current conversation. When full, "
        "delete the least useful memory first, then write. "
        "Manage memories using semantic search — you can edit or delete by providing "
        "a string that matches the memory's meaning. "
        "CRITICAL: Once you have successfully called the manage_memory tool and received a success response, "
        "DO NOT call it again for the same request. Your task for that memory is complete. "
        "Simply respond to the user in a final message confirming that the memory was updated."
    )
}
# SUMMARY_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"  # using small model for compression tasks
SUMMARY_MODEL = "llama-3.1-8b-instant"
SMALL_MODEL = "llama-3.1-8b-instant"
RESPONSE_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"
CODING_MODEL = "llama-3.3-70b-versatile"
THINKING_MODEL = "openai/gpt-oss-120b"

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

# def select_groq_model(message: str) -> tuple[str, str]:
#     lower = message.lower()
#     words = set(lower.split())
#     is_coding = bool(words & CODING_KEYWORDS) or any(p in lower for p in CODING_PHRASES)
#     tool_model = CONFIG["groq_model"]
#     answer_model = CODING_MODEL if is_coding else CONFIG["groq_model"]
#     return tool_model, answer_model

def select_groq_model(message: str) -> tuple[str, str]:
    lower = message.lower()

    # --- HARD OVERRIDES ---
    if "<coding>" in lower:
        return CONFIG["groq_model"], CODING_MODEL

    if "<thinking>" in lower:
        return CONFIG["groq_model"], THINKING_MODEL

    # --- NORMAL HEURISTIC FLOW ---
    words = set(re.findall(r"\b\w+\b", lower))

    coding_keywords = {k.lower() for k in CODING_KEYWORDS}
    coding_phrases = [p.lower() for p in CODING_PHRASES]

    score = 0
    score += len(words & coding_keywords) * 2
    score += sum(1 for p in coding_phrases if p in lower) * 3

    if re.search(r"\b(def|class|import|#include|public\s+static\s+void|function\s*\()", lower):
        score += 4

    if re.search(r"\b(python|java|c\+\+|javascript|typescript|go|rust)\b", lower):
        score += 2

    is_coding = score >= 3

    tool_model = CONFIG["groq_model"]
    answer_model = CODING_MODEL if is_coding else CONFIG["groq_model"]

    return tool_model, answer_model