import json
import logging

from config import (
    CONFIG, SUMMARIZE_THRESHOLD, SUMMARIZE_KEEP_LAST,
    SUMMARY_MODEL, get_model_setting,
)
import llm_provider
import prompts

log = logging.getLogger(__name__)


def _prune_history(history: list[dict], keep_last_n_tool_results: int = 2) -> list[dict]:
    pruned = []
    tool_result_count = 0
    for msg in reversed(history):
        if msg["role"] == "tool":
            tool_result_count += 1
            if tool_result_count > keep_last_n_tool_results:
                pruned.append({**msg, "content": "[pruned]"})
                continue
        elif msg["role"] == "assistant" and msg.get("content"):
            content_str = msg["content"]
            if len(content_str) > 2000:
                pruned.append({**msg, "content": content_str[:2000] + "\n... [truncated for token limits]"})
                continue
        pruned.append(msg)
    return list(reversed(pruned))


async def _maybe_summarize_history(provider: str, conversation_history: list[dict]) -> None:
    if provider == "ollama" or len(conversation_history) < SUMMARIZE_THRESHOLD:
        return

    summarize_settings = get_model_setting("summarize", "default")
    if not summarize_settings.get("enabled", True):
        return

    to_summarize = conversation_history[:-SUMMARIZE_KEEP_LAST]
    keep = conversation_history[-SUMMARIZE_KEEP_LAST:]
    summarizable = [m for m in to_summarize if m["role"] in ("user", "assistant") and m.get("content")]

    if not summarizable:
        return

    request_kwargs = {
        "model": CONFIG["model"] if provider == "ollama" else summarize_settings.get("model", SUMMARY_MODEL),
        "messages": [
            {"role": "system", "content": prompts.SUMMARIZE_HISTORY_PROMPT},
            {"role": "user", "content": json.dumps(summarizable)}
        ],
        "max_completion_tokens": summarize_settings.get("max_tokens", 300),
        "temperature": summarize_settings.get("temperature", 0.3),
    }
    reasoning_effort = summarize_settings.get("reasoning_effort")
    if isinstance(reasoning_effort, str) and reasoning_effort.strip():
        request_kwargs["reasoning_effort"] = reasoning_effort.strip()

    summary_response = await llm_provider.chat_completion_with_retry(provider, request_kwargs, CONFIG)

    summary = summary_response.choices[0].message.content
    conversation_history.clear()
    conversation_history.append({"role": "assistant", "content": f"[Previous conversation summary]: {summary}"})
    conversation_history.extend(keep)
    log.info("history summarized | new length=%d", len(conversation_history))


async def _summarize_tool_result(provider: str, tool_name: str, result: str) -> str:
    if provider == "ollama" or len(result) < 300 or tool_name != "web_tool":
        return result

    compact_settings = get_model_setting("compact", "default")
    if not compact_settings.get("enabled", True):
        return result

    try:
        request_kwargs = {
            "model": CONFIG["model"] if provider == "ollama" else compact_settings.get("model", SUMMARY_MODEL),
            "messages": [
                {"role": "system", "content": (
                    prompts.SUMMARIZE_TOOL_RESULT_PROMPT
                )},
                {"role": "user", "content": result}
            ],
            "max_completion_tokens": compact_settings.get("max_tokens", 200),
            "temperature": compact_settings.get("temperature", 0.1),
        }
        reasoning_effort = compact_settings.get("reasoning_effort")
        if isinstance(reasoning_effort, str) and reasoning_effort.strip():
            request_kwargs["reasoning_effort"] = reasoning_effort.strip()

        summary = await llm_provider.chat_completion_with_retry(provider, request_kwargs, CONFIG)
        summarized = summary.choices[0].message.content
        log.info("summarized tool result | %s | %d→%d chars", tool_name, len(result), len(summarized))
        return summarized
    except Exception as e:
        log.warning("summary failed, using raw result | %s", e)
        return result
