import asyncio
import json
import traceback
import logging
from types import SimpleNamespace
from typing import AsyncGenerator
import os
from pathlib import Path
from config import (
    CONFIG, MAX_HISTORY, SUMMARIZE_THRESHOLD, SUMMARIZE_KEEP_LAST,
    CODING_MODEL, SUMMARY_MODEL, THINKING_MODEL, load_memory,
    get_model_setting, CODING_KEYWORDS, CODING_PHRASES
)
import llm_provider
import prompts
import plans

log = logging.getLogger(__name__)

# ─── Pending edits persistence ─────────────────────────────────────────────
PENDING_EDITS_FILE = Path(__file__).parent / "pending_edits.json"

def _load_pending_edits() -> list[dict]:
    """Load pending edits from disk on startup."""
    if PENDING_EDITS_FILE.exists():
        try:
            with PENDING_EDITS_FILE.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                log.info("Loaded %d pending edit batch(es) from disk.", len(data))
                return data
        except Exception as e:
            log.warning("Failed to load pending edits file: %s", e)
    return []


def _save_pending_edits() -> None:
    """Persist pending edits to disk."""
    try:
        with PENDING_EDITS_FILE.open("w", encoding="utf-8") as f:
            json.dump(global_pending_edits, f, ensure_ascii=False)
    except Exception as e:
        log.warning("Failed to save pending edits: %s", e)


global_pending_edits: list[dict] = _load_pending_edits()
global_edit_log: list[str] = []


def _count_pending_edits() -> int:
    """Count total individual edits across all pending batches."""
    return sum(len(batch.get("edits", [])) for batch in global_pending_edits)


def _sse(event: str, data: str) -> str:
    payload = json.dumps({"event": event, "data": data})
    return f"data: {payload}\n\n"


def _extract_tool_call_from_text(content: str, available_tools: list[dict]) -> SimpleNamespace | None:
    """Detect when a model emits a tool call as plain text instead of a structured
    tool_call.  Handles patterns like:
        tool_name{key: "value", ...}
        tool_name({"key": "value", ...})
        tool_name{"key": "value"}
    Returns a SimpleNamespace matching the tool_call shape, or None."""
    import re

    if not content:
        return None

    tool_names = {t["function"]["name"] for t in available_tools}

    # Strip surrounding whitespace and any markdown fencing
    stripped = content.strip()
    # Some models (gemma4) emit <|"|> instead of actual quote characters
    stripped = stripped.replace('<|"|>', '"')
    log.debug("_extract_tool_call_from_text | checking content (len=%d): %r", len(stripped), stripped[:200])
    # Remove thinking/reasoning preamble — look for tool call anywhere in the text
    for name in tool_names:
        # Patterns: name{...}  or  name({...})  or  name( {...} )
        patterns = [
            rf'{re.escape(name)}\s*\(\s*(\{{.*\}})\s*\)',   # name({...})
            rf'{re.escape(name)}\s*(\{{.*\}})',              # name{...}
        ]
        for pattern in patterns:
            match = re.search(pattern, stripped, re.DOTALL)
            if match:
                json_str = match.group(1)
                try:
                    args = json.loads(json_str)
                    if isinstance(args, dict):
                        return SimpleNamespace(
                            id=f"call_{name}_extracted",
                            function=SimpleNamespace(
                                name=name,
                                arguments=json.dumps(args),
                            ),
                        )
                except json.JSONDecodeError:
                    # Try fixing common issues: unquoted keys, single quotes
                    try:
                        fixed = json_str.replace("'", '"')
                        # Quote unquoted keys: {action:"v"} -> {"action":"v"}
                        fixed = re.sub(r'(\{|,)\s*([a-zA-Z_]\w*)\s*:', r'\1"\2":', fixed)
                        args = json.loads(fixed)
                        if isinstance(args, dict):
                            return SimpleNamespace(
                                id=f"call_{name}_extracted",
                                function=SimpleNamespace(
                                    name=name,
                                    arguments=json.dumps(args),
                                ),
                            )
                    except json.JSONDecodeError:
                        continue

    return None




def _prune_history(history: list[dict], keep_last_n_tool_results: int = 2) -> list[dict]:
    pruned = []
    tool_result_count = 0
    for msg in reversed(history):
        if msg["role"] == "tool":
            # EXEMPT pending edits from pruning
            if msg.get("content") and "edit pending approval" in msg.get("content", ""):
                pruned.append(msg)
                continue
                
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
    if provider == "ollama" or len(result) < 300 or tool_name not in ("web_search", "fetch_webpage"):
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


async def generate_and_save_title(conversation_id: str, first_message: str):
    import db
    try:
        provider = llm_provider.get_effective_provider(CONFIG)
        if provider == "ollama":
            return
            
        title_prompt = prompts.TITLE_GENERATION_PROMPT_TEMPLATE.format(first_message=first_message)
        title = ""
        provider = llm_provider.get_effective_provider(CONFIG)
        title_settings = get_model_setting("title", "default")
        request_kwargs = {
            "model": CONFIG["model"] if provider == "ollama" else llm_provider.get_model_name_for_provider(title_settings, provider, SUMMARY_MODEL),
            "messages": [
                {"role": "user", "content": title_prompt}
            ],
            "max_completion_tokens": title_settings.get("max_tokens", 200),
            "temperature": title_settings.get("temperature", 0.5),
        }
        reasoning_effort = title_settings.get("reasoning_effort")
        if isinstance(reasoning_effort, str) and reasoning_effort.strip():
            request_kwargs["reasoning_effort"] = reasoning_effort.strip()

        response = await llm_provider.chat_completion_with_retry(provider, request_kwargs, CONFIG)
        title = response.choices[0].message.content.strip()
        
        title = title.strip().strip('"').strip("'").strip()
        if title:
            title = title[:50]  # truncate to prevent layout issues
            db.update_conversation_title(conversation_id, title)
            log.info(f"Generated title for {conversation_id}: {title}")
    except Exception as e:
        log.error(f"Failed to generate title for {conversation_id}: {e}")


async def agent_loop(user_message: str, mcp, model_override: str = "default", conversation_id: str = None, ollama_thinking: bool = False) -> AsyncGenerator[str, None]:
    global global_pending_edits
    import db
    try:
        if conversation_id:
            yield _sse("conversation_id", conversation_id)
        yield _sse("status", "loop started")
        clean_message = user_message.replace("<thinking>", "").replace("<coding>", "").strip()
        
        # Load history and save new user message
        conversation_history = db.get_messages(conversation_id)
        db.add_message(conversation_id, "user", clean_message)
        conversation_history.append({"role": "user", "content": clean_message})
        yield _sse("status", "history appended")

        # Background title generation if it's currently a new chat
        title = db.get_conversation_title(conversation_id)
        if title == "New Chat" or not title:
            asyncio.create_task(generate_and_save_title(conversation_id, clean_message))

        resolved_provider = db.resolve_conversation_provider(
            conversation_id,
            llm_provider.get_effective_provider(CONFIG)
        )
        use_remote_provider = resolved_provider in {"groq", "openrouter"}

        if use_remote_provider:
            tool_settings = get_model_setting("tool", "default")
            if model_override == "coding":
                tool_model, answer_model = (
                    llm_provider.get_model_name_for_provider(tool_settings, resolved_provider, CONFIG.get("groq_model")),
                    llm_provider.get_model_name_for_provider(get_model_setting("coding", "answer"), resolved_provider, CODING_MODEL),
                )
            elif model_override == "thinking":
                tool_model, answer_model = (
                    llm_provider.get_model_name_for_provider(tool_settings, resolved_provider, CONFIG.get("groq_model")),
                    llm_provider.get_model_name_for_provider(get_model_setting("thinking", "answer"), resolved_provider, THINKING_MODEL),
                )
            else:
                tool_model, answer_model = llm_provider.select_model(
                    user_message, CONFIG, get_model_setting, CODING_MODEL, THINKING_MODEL, CONFIG.get("groq_model"), CODING_KEYWORDS, CODING_PHRASES
                )

            if answer_model != tool_model:
                yield _sse("model_upgrade", answer_model)
        else:
            default_settings = get_model_setting("default", "default")
            tool_model = default_settings.get("model", CONFIG["model"])
            answer_model = default_settings.get("model", CONFIG["model"])

        log.info("agent_loop: starting")
        log.info("agent_loop: provider=%s | ollama_thinking=%s | model_override=%s", resolved_provider, ollama_thinking, model_override)
        mcp_tools = await mcp.list_tools()
        log.info("agent_loop: mcp_tools fetched")
        yield _sse("status", f"got {len(mcp_tools)} tools")

        ollama_tools = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("inputSchema", {}),
                }
            }
            for t in mcp_tools
        ]
        log.info("agent_loop: tools mapped")
        yield _sse("status", f"calling {resolved_provider}")

        search_count = 0
        email_count = 0
        memory_count = 0
        tool_count = 0
        last_read_path = ""
        max_searches = CONFIG["max_searches"]
        max_emails = CONFIG["max_emails"]
        max_tools = CONFIG["max_tools"]
        max_memory = 3

        while True:
            try:
                # Skip pruning on local (ollama) to preserve KV cache prefix
                if use_remote_provider:
                    trimmed_history = _prune_history(conversation_history[-MAX_HISTORY:])
                else:
                    trimmed_history = conversation_history[-MAX_HISTORY:]
                is_final_call = len(trimmed_history) > 0 and trimmed_history[-1]["role"] == "tool"
                # current_model = answer_model if is_final_call else tool_model
                current_model = answer_model # i give up trying to optimize this single call.
                current_settings = get_model_setting("answer", "default")
                if model_override == "coding":
                    current_settings = get_model_setting("coding", "answer")
                elif model_override == "thinking":
                    current_settings = get_model_setting("thinking", "answer")

                if use_remote_provider:
                    await _maybe_summarize_history(resolved_provider, conversation_history)

                memory = load_memory()
                system = prompts.SYSTEM_PROMPT + "\n\n" + prompts.MEMORY_MANAGEMENT_PROMPT
                if memory:
                    system = system + "\n\n" + memory
                    
                if global_edit_log:
                    system += "\n\nEdit Log:\n" + "\n".join(global_edit_log)

                yield _sse("model", json.dumps({
                    "provider": resolved_provider,
                    "model": current_model if use_remote_provider else CONFIG["model"]
                }))

                last_was_pending_edit = len(trimmed_history) > 0 and trimmed_history[-1].get("role") == "tool" and trimmed_history[-1].get("content") == "edit pending approval. provide a short summary of the changes."

                if last_was_pending_edit:
                    available_tools = []
                elif use_remote_provider:
                    # Remote: filter tools to save tokens (no KV cache to preserve)
                    available_tools = []
                    for t in ollama_tools:
                        if tool_count >= max_tools:
                            break
                        func_name = t["function"]["name"]
                        if func_name in ("web_search", "fetch_webpage") and search_count >= max_searches:
                            continue
                        if func_name == "draft_email" and email_count >= max_emails:
                            continue
                        if func_name == "save_user_preference" and memory_count >= max_memory:
                            continue
                        available_tools.append(t)
                else:
                    # Local/Ollama: always pass full tool list to preserve KV cache.
                    # Limits are enforced at execution time via error messages.
                    available_tools = ollama_tools

                request_kwargs = {
                    "model": current_model if use_remote_provider else CONFIG["model"],
                    "messages": [
                        {"role": "system", "content": system},
                        *trimmed_history,
                    ],
                    "tools": available_tools if available_tools else [],
                    "temperature": current_settings.get("temperature", CONFIG["temperature"]),
                    "max_completion_tokens": current_settings.get("max_tokens", CONFIG["max_tokens"]),
                }
                if use_remote_provider:
                    reasoning_effort = current_settings.get("reasoning_effort")
                    if isinstance(reasoning_effort, str) and reasoning_effort.strip():
                        request_kwargs["reasoning_effort"] = reasoning_effort.strip()
                else:
                    # Ollama thinking: per-request flag wins, then per-role setting,
                    # then global config fallback (handled inside _build_ollama_payload).
                    # IMPORTANT: never send think:True when tools are available — most
                    # Ollama models (including gemma4) silently drop tool calls when
                    # thinking mode is active, returning empty content instead.
                    role_thinking = current_settings.get("thinking", False)
                    want_thinking = ollama_thinking or model_override == "thinking" or role_thinking
                    has_tools = bool(available_tools)
                    if want_thinking and not has_tools:
                        request_kwargs["thinking"] = True
                        log.debug("ollama thinking enabled (no tools in this turn)")
                    elif want_thinking and has_tools:
                        log.debug("ollama thinking suppressed — tools are present, think:True would break tool calls")

                try:
                    log.info("agent_loop: calling LLM provider=%s with %d tools", resolved_provider, len(request_kwargs.get("tools", [])))

                    if not use_remote_provider:
                        # ── Ollama streaming path ──────────────────────────────────────
                        assembled = None
                        verbose_stats = None
                        content_started = False
                        thinking_started = False
                        token_count = 0
                        thinking_count = 0
                        async for stream_chunk in llm_provider._ollama_stream_chat_completion(request_kwargs, CONFIG):
                            choice = stream_chunk.choices[0]
                            delta = choice.delta

                            if delta.thinking:
                                yield _sse("thinking_token", delta.thinking)
                                thinking_count += len(delta.thinking)
                                thinking_started = True

                            if delta.content:
                                yield _sse("token", delta.content)
                                content_started = True
                                token_count += len(delta.content)

                            if choice.finish_reason is not None:
                                assembled = stream_chunk.assembled
                                verbose_stats = assembled.verbose_stats if assembled else None
                                log.info(
                                    "ollama stream done | finish_reason=%s | content_chars=%d | thinking_chars=%d | tool_calls_raw=%s",
                                    choice.finish_reason,
                                    token_count,
                                    thinking_count,
                                    assembled.tool_calls_raw if assembled else [],
                                )
                                break

                        if assembled is None:
                            log.warning("ollama stream ended with no done chunk — treating as empty response")
                            assembled_content = ""
                            assembled_thinking = None
                            assembled_tool_calls = []
                        else:
                            assembled_content = assembled.content
                            assembled_thinking = assembled.thinking
                            assembled_tool_calls = llm_provider._parse_ollama_tool_calls_raw(
                                assembled.tool_calls_raw
                            )
                            log.info(
                                "ollama assembled | content=%r | tool_calls=%s",
                                (assembled_content or "")[:120],
                                [tc.function.name for tc in assembled_tool_calls],
                            )

                        # Fallback: gemma4 and some other models correctly return
                        # tool_calls in non-streaming mode but emit empty content with
                        # no tool_calls when streamed. If we got nothing useful from
                        # the stream, retry with a non-streaming call to recover.
                        if not assembled_tool_calls and not assembled_content and assembled_thinking:
                            log.warning(
                                "ollama stream returned thinking-only with no content/tools — "
                                "falling back to non-streaming call to recover tool_calls"
                            )
                            # Content was already streamed as empty so nothing to clear,
                            # but reset content_started so we don't skip the token emit below.
                            content_started = False
                            fallback_resp = await llm_provider._ollama_chat_completion(request_kwargs, CONFIG)
                            fallback_msg = fallback_resp.choices[0].message
                            verbose_stats = getattr(fallback_resp, "verbose_stats", verbose_stats)
                            assembled_content = fallback_msg.content or ""
                            assembled_thinking = fallback_msg.thinking
                            assembled_tool_calls = list(fallback_msg.tool_calls or [])
                            log.info(
                                "ollama fallback result | content=%r | tool_calls=%s",
                                (assembled_content or "")[:120],
                                [tc.function.name for tc in assembled_tool_calls],
                            )

                        # Fallback: some models (especially gemma4) emit tool calls
                        # as plain text content instead of structured tool_calls.
                        # Detect patterns like: tool_name{...json...} or tool_name({...})
                        # and convert them into real tool calls.
                        if not assembled_tool_calls and assembled_content:
                            extracted = _extract_tool_call_from_text(assembled_content, ollama_tools)
                            if extracted:
                                log.warning(
                                    "ollama emitted tool call as text — extracting: %s",
                                    extracted.function.name,
                                )
                                assembled_tool_calls = [extracted]
                                assembled_content = None
                                # Tell the UI to discard the text tokens already streamed
                                if content_started:
                                    yield _sse("clear_tokens", "")
                                    content_started = False

                        msg = SimpleNamespace(
                            content=assembled_content,
                            tool_calls=assembled_tool_calls,
                            thinking=assembled_thinking,
                        )

                    else:
                        # ── Remote provider (groq / openrouter) — non-streaming ────────
                        response = await llm_provider.chat_completion_with_retry(resolved_provider, request_kwargs, CONFIG)
                        msg = response.choices[0].message
                        verbose_stats = None
                        content_started = False  # remote path doesn't pre-stream tokens

                    log.info("agent_loop: LLM responded")
                except Exception as e:
                    if hasattr(e, 'response'):
                        log.error("Provider 400 body: %s", e.response.text)
                    raise

                tool_calls = msg.tool_calls or []
                content = msg.content

                if tool_calls:
                    content = None
                    # If we already streamed some content tokens live before discovering
                    # the model wanted to call tools, tell the UI to discard them.
                    if content_started:
                        yield _sse("clear_tokens", "")
                else:
                    content = content or ""

                log.info("%s response | content=%s | tool_calls=%s", resolved_provider, content, tool_calls)
                yield _sse("status", f"{resolved_provider} responded")

                if not tool_calls:
                    # Only persist non-empty assistant messages. An empty response
                    # (e.g. model returned only thinking tokens, fallback also gave
                    # nothing useful) should not be written to history as it will
                    # corrupt future turns.
                    thinking_text = getattr(msg, 'thinking', None) or None
                    if content:
                        conversation_history.append({"role": "assistant", "content": content})
                        db.add_message(conversation_id, "assistant", content, thinking_content=thinking_text)

                    if content_started:
                        # Ollama already streamed tokens live — just emit stats and done
                        pass
                    else:
                        # Remote providers: fake-stream word by word
                        for word in content.split(" "):
                            yield _sse("token", word + " ")
                            await asyncio.sleep(0.01)

                    # Emit verbose stats for Ollama responses
                    if verbose_stats:
                        yield _sse("ollama_stats", json.dumps(verbose_stats))

                    yield _sse("done", json.dumps({
                        "searches_used": search_count,
                        "emails_used": email_count,
                        "tools_used": tool_count,
                        "max_searches": max_searches,
                        "max_emails": max_emails,
                        "max_tools": max_tools
                    }))
                    return

                tool_calls_for_history = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments if tc.function.arguments and tc.function.arguments != "null" else "{}"
                        }
                    }
                    for tc in tool_calls
                ]

                conversation_history.append({"role": "assistant", "content": None, "tool_calls": tool_calls_for_history})
                db.add_message(conversation_id, "assistant", None, tool_calls=tool_calls_for_history)

                # Buffer all tool result messages for this batch, then append
                # them all at the end so they land contiguously at the tail of
                # the messages array (important for pending edits and correct
                # multi-tool ordering).
                pending_tool_messages: list[tuple[str, str]] = []  # (tool_call_id, content)

                for tc in tool_calls:
                    name = tc.function.name
                    raw_args = tc.function.arguments
                    if isinstance(raw_args, str):
                        args = json.loads(raw_args) if raw_args and raw_args != "null" else {}
                    else:
                        args = raw_args or {}

                    yield _sse("tool_requested", json.dumps({"tool": name, "args": args}))

                    if search_count >= max_searches and name in ("web_search", "fetch_webpage"):
                        yield _sse("status", f"Search cap ({max_searches}) reached, answering from context...")
                        pending_tool_messages.append((tc.id, f"Search limit of {max_searches} reached."))
                        continue

                    if email_count >= max_emails and name == "draft_email":
                        yield _sse("status", f"Email cap ({max_emails}) reached...")
                        pending_tool_messages.append((tc.id, f"Email drafting limit of {max_emails} reached. You cannot draft any more emails in this turn. Please proceed to answer the user based on the drafts already opened."))
                        continue

                    if memory_count >= max_memory and name == "save_user_preference":
                        yield _sse("status", f"Memory cap ({max_memory}) reached...")
                        pending_tool_messages.append((tc.id, f"Memory management limit of {max_memory} reached. No more memory edits allowed this turn. Please proceed to answer the user."))
                        continue

                    if name == "propose_edit" and _count_pending_edits() >= 5:
                        yield _sse("status", "Pending edits cap (5) reached...")
                        pending_tool_messages.append((tc.id, "Maximum pending edits (5) reached. The user must approve or reject existing edits before you can propose more. Please proceed to answer the user."))
                        continue

                    if tool_count >= max_tools:
                        yield _sse("status", f"Total tool cap ({max_tools}) reached...")
                        pending_tool_messages.append((tc.id, f"Total tool limit of {max_tools} reached. No more tools can be called. Please proceed to answer the user with the information you already have."))
                        continue

                    event_payload = {"tool": name, "args": args}
                    if name == "web_search":
                        event_payload["count"] = search_count + 1
                        event_payload["max"] = max_searches
                        event_payload["label"] = f'searching: "{args.get("query", "")}" ({search_count + 1}/{max_searches})'
                    elif name == "fetch_webpage":
                        event_payload["label"] = f'fetching: {args.get("url", "")}'
                    elif name == "list_files":
                        event_payload["label"] = f'listing workspace files, with option {args.get("topic", "all")}'
                    elif name == "read_file":
                        event_payload["label"] = f'reading: {args.get("path", "")}, with {args.get("count")} lines from lines {args.get("start")}'
                    elif name == "calculate":
                        event_payload["label"] = f'calculating: {args.get("expression", "")}'
                    elif name == "recent_events":
                        event_payload["label"] = f'getting recent info for: {args.get("infoType", "")}, for/about {args.get("details","")}'
                    elif name == "draft_email":
                        event_payload["count"] = email_count + 1
                        event_payload["max"] = max_emails
                        event_payload["label"] = f'drafting email to {args.get("to","NA")}, about {args.get("subject","NA")} ({email_count + 1}/{max_emails})'
                    elif name == "search_semantic":
                        event_payload["label"] = f'conceptual search: "{args.get("query", "")}"'
                    else:
                        event_payload["label"] = f'{name}: {json.dumps(args)}'

                    yield _sse("searching", json.dumps(event_payload))
                    log.info("tool_call | %s | args=%s", name, json.dumps(args))

                    # Intercept plan tool (needs conversation_id, not MCP)
                    if name == "plan":
                        action = args.get("action")
                        if action == "create":
                            plan = plans.create_plan(conversation_id, args["goal"], args["steps"])
                            result = json.dumps({"status": "plan_created", "goal": plan["goal"], "steps": len(plan["steps"])})
                            yield _sse("plan_update", json.dumps({"plan": plan}))
                        elif action == "update":
                            plan = plans.update_plan(conversation_id, args.get("step_index"), args.get("status"), args.get("new_steps"))
                            if plan:
                                result = json.dumps({"status": "plan_updated", "plan": plans.format_plan_status(plan)})
                                yield _sse("plan_update", json.dumps({"plan": plan}))
                            else:
                                result = "No active plan for this conversation."
                        else:
                            result = f"Unknown plan action: {action}"
                    else:
                        # Track last read_file path for propose_edit fallback
                        if name == "read_file" and args.get("path"):
                            last_read_path = args["path"]

                        # Auto-fill missing 'path' in propose_edit from last read_file
                        if name == "propose_edit" and last_read_path and not args.get("path"):
                            args["path"] = last_read_path
                            log.info("propose_edit: auto-filled missing path with '%s'", last_read_path)

                        result = await mcp.call_tool(name, args)

                    if name == "fetch_webpage":
                        try:
                            lines = result.split("\n")
                            metadata_line = next((l for l in lines if l.startswith("__LINK_METADATA__:")), None)
                            if metadata_line:
                                metadata = json.loads(metadata_line.split(":", 1)[1])
                                yield _sse("link_card", json.dumps(metadata))
                                result = "\n".join(l for l in lines if not l.startswith("__LINK_METADATA__:"))
                        except Exception:
                            pass

                    try:
                        lines = result.split("\n")
                        action_line = next((l for l in lines if l.strip().startswith("__ACTION_BUTTON__:")), None)
                        if action_line:
                            log.debug(f"Found action button line: {action_line}")
                            action_data = json.loads(action_line.split(":", 1)[1])
                            yield _sse("action_button", json.dumps(action_data))
                            result = "\n".join(l for l in lines if not l.strip().startswith("__ACTION_BUTTON__:"))
                    except Exception as e:
                        log.error(f"Error processing action button: {e}")

                    if name == "propose_edit":
                        try:
                            parsed = json.loads(result)
                            if parsed.get("status") == "pending_approval":
                                edits_list = parsed.get("edits", [])
                                batch_entry = {
                                    "toolCallId": tc.id,
                                    "edits": edits_list,
                                    "summary": parsed.get("summary"),
                                    "conversation_id": conversation_id
                                }
                                global_pending_edits.append(batch_entry)
                                _save_pending_edits()
                                yield _sse("propose_edit", json.dumps(batch_entry))
                                result = "edit pending approval. provide a short summary of the changes for the user."
                        except Exception:
                            # result already contains the REJECTED message from the tool — 
                            # pass it through so the model sees the error details
                            yield _sse("status", "Edit proposed but failed validation. Agent will retry.")
                            if "REJECTED" not in result:
                                result = (
                                    f"REJECTED: Edit failed validation. Error: {result}. "
                                    f"Remember: propose_edit takes a single edit with 'path' (e.g. 'draft.txt'), "
                                    f"'action', 'anchor' (short — one line max, required unless action is 'create'), and 'content'."
                                )

                    if use_remote_provider and name in ("web_search", "fetch_webpage"):
                        result = await _summarize_tool_result(resolved_provider, name, result)
                        yield _sse("status", f"summarized {name} result")

                    if name in ("web_search", "fetch_webpage"):
                        search_count += 1
                    if name == "draft_email":
                        email_count += 1
                    if name == "save_user_preference":
                        memory_count += 1

                    if name not in ("plan",):
                        tool_count += 1
                    yield _sse("search_result", json.dumps({"tool": name, "count": tool_count}))

                    pending_tool_messages.append((tc.id, result))

                # Append all tool results to history at the end of the batch
                for tool_call_id, content in pending_tool_messages:
                    conversation_history.append({
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": content,
                    })
                    db.add_message(conversation_id, "tool", content, tool_call_id=tool_call_id)

                # Append plan status to the last tool result (ephemeral, not persisted)
                active_plan = plans.get_plan(conversation_id)
                if active_plan and conversation_history and conversation_history[-1]["role"] == "tool":
                    plan_status = plans.format_plan_status(active_plan)
                    conversation_history[-1]["content"] += f"\n\n{plan_status}"
            except Exception as e:
                if resolved_provider == "groq" and hasattr(e, 'response'):
                    log.error("Groq 400 body: %s", e.response.text)
                if "tool_use_failed" in str(e):
                    log.warning("tool_use_failed — retrying without tools")
                    fallback_kwargs = {
                        "model": current_model if use_remote_provider else CONFIG["model"],
                        "messages": [
                            {"role": "system", "content": system},
                            *trimmed_history,
                        ],
                        "temperature": current_settings.get("temperature", CONFIG["temperature"]),
                        "max_completion_tokens": current_settings.get("max_tokens", CONFIG["max_tokens"]),
                    }
                    if not use_remote_provider and request_kwargs.get("thinking"):
                        fallback_kwargs["thinking"] = True

                    if not use_remote_provider:
                        fallback_assembled = None
                        fallback_stats = None
                        async for fb_chunk in llm_provider._ollama_stream_chat_completion(fallback_kwargs, CONFIG):
                            fb_choice = fb_chunk.choices[0]
                            if fb_chunk.choices[0].delta.thinking:
                                yield _sse("thinking_token", fb_chunk.choices[0].delta.thinking)
                            if fb_chunk.choices[0].delta.content:
                                yield _sse("token", fb_chunk.choices[0].delta.content)
                            if fb_choice.finish_reason is not None:
                                fallback_assembled = fb_chunk.assembled
                                fallback_stats = fallback_assembled.verbose_stats if fallback_assembled else None
                                break
                        content = fallback_assembled.content if fallback_assembled else ""
                        fallback_thinking = fallback_assembled.thinking if fallback_assembled else None
                        if fallback_stats:
                            yield _sse("ollama_stats", json.dumps(fallback_stats))
                    else:
                        response = await llm_provider.chat_completion_with_retry(
                            resolved_provider, fallback_kwargs, CONFIG
                        )
                        content = response.choices[0].message.content or ""
                        fallback_thinking = None
                        for word in content.split(" "):
                            yield _sse("token", word + " ")
                            await asyncio.sleep(0.01)

                    if content:
                        conversation_history.append({"role": "assistant", "content": content})
                        db.add_message(conversation_id, "assistant", content, thinking_content=fallback_thinking)
                    yield _sse("done", json.dumps({
                        "searches_used": search_count,
                        "emails_used": email_count,
                        "tools_used": tool_count,
                        "max_searches": max_searches,
                        "max_emails": max_emails,
                        "max_tools": max_tools
                    }))
                    return
                raise

    except Exception as e:
        log.error("agent_loop error: %s", traceback.format_exc())
        yield _sse("error", traceback.format_exc())
        yield _sse("done", "{}")
