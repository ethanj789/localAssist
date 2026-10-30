import logging

from config import load_memory
import prompts

log = logging.getLogger(__name__)


def build_system_prompt() -> str:
    """Assemble the system prompt exactly as a live turn does.

    Single source of truth for the system string so the KV-cache warmup
    (server.py) and the real agent turn stay byte-identical. The edit log is no
    longer embedded here — pending edits are DB truth and applied edits leave
    their real tool result in history, so nothing edit-related belongs in the
    system prompt (which would nuke the KV cache on every change). Memory is the
    one accepted KV nuker and stays.
    """
    memory = load_memory()
    system = prompts.SYSTEM_PROMPT + "\n\n" + prompts.MEMORY_MANAGEMENT_PROMPT
    if memory:
        system = system + "\n\n" + memory
    return system


def build_ollama_tools(mcp_tools: list[dict]) -> list[dict]:
    """Map MCP tool defs into the Ollama tools array shape. Shared by the
    agent turn and the KV-cache warmup so the rendered prompt prefix matches."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("inputSchema", {}),
            },
        }
        for t in mcp_tools
    ]
