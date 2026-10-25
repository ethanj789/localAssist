import asyncio
import json
import time
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

# Tools whose call must be confirmed by the user (break-and-resume): when one
# is called, the turn ends with the tool call left UNANSWERED in the DB and the
# UI surfaces a pending approval derived from DB truth. See
# db.get_pending_tool_call (which keeps a matching copy of this set to avoid an
# import cycle).
REQUIRES_CONFIRMATION = {"propose_edit"}

# Contract strings (frozen — see design doc). Kept as module constants so the
# propose/resume paths and the endpoints stay byte-identical.
AUTO_REJECT_EXTRA_EDIT = (
    "Only one edit can be pending approval at a time. This additional edit was "
    "NOT queued or saved. After the current pending edit is approved or "
    "rejected, propose this edit again. (Note: edits are resolved one at a time "
    "as the user approves them, not once per user message.)"
)


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


def _sse(event: str, data: str) -> str:
    payload = json.dumps({"event": event, "data": data})
    return f"data: {payload}\n\n"


def _file_page_b64s(conversation_id: str, file_objs: list[dict]) -> list[str]:
    """Build the ordered bare-base64 page images for a turn's file attachments.

    Deterministic ordering (critical for KV-cache reuse): iterate the turn's
    files in stored order; within each file iterate pages 1..N. The page
    identity is carried by the text labels already spliced into the message
    content, so even multi-file turns map correctly. Missing page files are
    skipped so a deleted file can't break the turn."""
    import file_store

    b64s: list[str] = []
    for obj in file_objs or []:
        if not isinstance(obj, dict):
            continue
        file_id = obj.get("path")
        pages = obj.get("pages", 0)
        if not file_id or not pages:
            continue
        for rel in file_store.page_rel_paths(conversation_id, file_id, pages):
            b64 = file_store.load_as_base64(rel)
            if b64:
                b64s.append(b64)
    return b64s


def _file_text_block(conversation_id: str, file_objs: list[dict]) -> str:
    """Build the page-labeled text block for a turn's file attachments, read
    from each file's ``text.txt`` on disk (in stored order).

    This is MODEL-ONLY context: it is appended to the message content sent to
    the model, but never stored in the DB `content` (which holds only the user's
    typed text, for display). Reconstructed deterministically from the same
    on-disk `text.txt` every turn so the KV-cache prefix stays byte-identical.
    Mirrors how page images are rebuilt from disk in _file_page_b64s."""
    import file_store

    blocks: list[str] = []
    for obj in file_objs or []:
        if not isinstance(obj, dict) or not obj.get("hasText"):
            continue
        file_id = obj.get("path")
        if not file_id:
            continue
        text = file_store.load_text(conversation_id, file_id)
        if text and text.strip():
            blocks.append(text.strip())
    return "\n\n".join(blocks)


def _apply_file_context(conversation_id: str, msg: dict) -> None:
    """Fold a user message's attachment page-images AND extracted text into the
    message as MODEL-ONLY context, leaving the stored `content` (display text)
    untouched. Used for both the current turn and rehydrated history so the two
    paths build an identical model message. Keeps the rebuild deterministic for
    KV-cache reuse."""
    file_objs = msg.get("files")
    if not file_objs:
        return
    # Append the attachment text block to the content the model sees.
    text_block = _file_text_block(conversation_id, file_objs)
    if text_block:
        base = (msg.get("content") or "").strip()
        msg["content"] = f"{base}\n\n{text_block}" if base else text_block
    elif not (msg.get("content") or "").strip():
        # A file with no text layer and no user text — give the model a hint
        # rather than an empty content string.
        msg["content"] = "[The user sent a file attachment with no text.]"


def _rehydrate_history_files(conversation_id: str, history: list[dict]) -> None:
    """Re-attach page images for OLDER user messages reloaded from history.

    get_messages() returns a user message's attachments as a list of lean file
    objects under `files`. For each, we rebuild both the model-only context:
    the rendered page images (bare base64, loaded from disk in deterministic
    page order) set on the message's `images` field — the channel Ollama
    consumes — and the extracted text, folded back into the message content via
    _apply_file_context (read from disk; the DB `content` holds only the user's
    typed display text).

    Keeping this rebuild byte-identical turn to turn is what lets Ollama /
    llama.cpp keep the longest-unchanged-prefix KV cache valid."""
    for msg in history:
        if msg.get("role") != "user":
            continue

        file_objs = msg.get("files")
        if file_objs:
            # Fold the attachment's extracted TEXT into the model-visible content
            # (read from disk; never stored in the DB `content`, which is the
            # user's typed text for display only).
            _apply_file_context(conversation_id, msg)
            b64s = _file_page_b64s(conversation_id, file_objs)
            if b64s:
                msg["images"] = b64s
            else:
                msg.pop("images", None)
            # `files` is an internal DB/UI field, not part of Ollama's schema.
            msg.pop("files", None)


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


def _resolve_turn_models(resolved_provider: str, use_remote_provider: bool, model_override: str, user_message: str) -> tuple[str, str]:
    """Resolve (tool_model, answer_model) for a turn — factored out so both the
    live agent turn and the resume-after-confirmation path pick models the same
    way. Does NOT emit the `model_upgrade` SSE; the caller compares the two and
    emits it when they differ (only the live path does)."""
    if use_remote_provider:
        tool_settings = get_model_setting("tool", "default")
        if model_override == "coding":
            return (
                llm_provider.get_model_name_for_provider(tool_settings, resolved_provider, CONFIG.get("groq_model")),
                llm_provider.get_model_name_for_provider(get_model_setting("coding", "answer"), resolved_provider, CODING_MODEL),
            )
        elif model_override == "thinking":
            return (
                llm_provider.get_model_name_for_provider(tool_settings, resolved_provider, CONFIG.get("groq_model")),
                llm_provider.get_model_name_for_provider(get_model_setting("thinking", "answer"), resolved_provider, THINKING_MODEL),
            )
        else:
            return llm_provider.select_model(
                user_message, CONFIG, get_model_setting, CODING_MODEL, THINKING_MODEL, CONFIG.get("groq_model"), CODING_KEYWORDS, CODING_PHRASES
            )
    else:
        default_settings = get_model_setting("default", "default")
        model = default_settings.get("model", CONFIG["model"])
        return (model, model)


async def _run_tool_loop(
    conversation_history: list[dict],
    conversation_id: str,
    mcp,
    resolved_provider: str,
    use_remote_provider: bool,
    ollama_tools: list[dict],
    answer_model: str,
    tool_model: str,
    model_override: str,
    ollama_thinking: bool,
) -> AsyncGenerator[str, None]:
    """Shared tool loop used by both the live agent turn (``agent_loop``) and
    the resume path (``resume_after_confirmation``).

    Owns the per-turn counters, the ``while True:`` provider/tool loop, and the
    outer tool-failure fallback. For the normal (no confirm-required tool) path
    its behavior is byte-for-byte identical to the previous inline loop.

    Confirm-required gating (break-and-resume): within a tool batch, non-confirm
    tools run first (their results are appended). The FIRST confirm-required
    call is left UNANSWERED — we emit a ``confirm_required`` SSE (resolving the
    edit once for the preview) and ``return``, ending the generator with that
    assistant ``tool_calls`` message dangling in the DB. Any ADDITIONAL
    confirm-required calls in the same batch are answered with the Option-P
    auto-reject result (so the message array stays valid)."""
    import db

    search_count = 0
    email_count = 0
    memory_count = 0
    tool_count = 0
    # Per-turn counter for edits whose anchor didn't resolve at the gate. Each
    # such edit is auto-rejected server-side with the specific error (so the
    # model can re-propose) WITHOUT bothering the UI. After the cap we stop
    # auto-retrying and surface the failed preview to the UI instead, so a model
    # that can't land an anchor doesn't spin forever.
    invalid_edit_retries = 0
    MAX_INVALID_EDIT_RETRIES = 2
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

            system = build_system_prompt()

            yield _sse("model", json.dumps({
                "provider": resolved_provider,
                "model": current_model if use_remote_provider else CONFIG["model"]
            }))

            if use_remote_provider:
                # Remote: filter tools to save tokens (no KV cache to preserve)
                available_tools = []
                for t in ollama_tools:
                    if tool_count >= max_tools:
                        break
                    func_name = t["function"]["name"]
                    if func_name == "web_tool" and search_count >= max_searches:
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
                        log.debug(
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
                        log.debug(
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

                log.debug("agent_loop: LLM responded")
            except Exception as e:
                resp = getattr(e, "response", None)
                if resp is not None:
                    try:
                        body = resp.text
                    except Exception:
                        # Streamed responses raise ResponseNotRead on .text
                        # unless the body was already read. Fall back to the
                        # raw (possibly already-read) content.
                        try:
                            body = resp.content.decode(errors="replace")
                        except Exception:
                            body = f"<unreadable response body; status={getattr(resp, 'status_code', '?')}>"
                    log.error("Provider error body: %s", body)
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

            _tc_names = [getattr(tc.function, "name", "?") for tc in (tool_calls or [])]
            log.info(
                "%s response | content_chars=%d | content_preview=%r | tool_calls=%s",
                resolved_provider,
                len(content or ""),
                (content or "")[:200],
                _tc_names,
            )
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

            # ── Confirm-required gating (Option P) ───────────────────────────
            # Partition the batch: the FIRST confirm-required call gates the turn
            # (left unanswered); every ADDITIONAL confirm-required call is
            # auto-rejected so the message array stays valid. Non-confirm tools
            # run normally in the loop below FIRST so their results (and
            # last_read_path) are ready before we build the preview / resume.
            gate_tc = None          # the first confirm-required tool call
            for tc in tool_calls:
                if tc.function.name in REQUIRES_CONFIRMATION:
                    if gate_tc is None:
                        gate_tc = tc
                    else:
                        # Extra confirm call — answer it with the Option-P message.
                        yield _sse("status", "Additional edit auto-rejected (one pending at a time).")
                        pending_tool_messages.append((tc.id, AUTO_REJECT_EXTRA_EDIT))

            from tools.edit import strip_invalid_json_escapes

            for tc in tool_calls:
                name = tc.function.name
                raw_args = tc.function.arguments
                if isinstance(raw_args, str):
                    args = json.loads(raw_args) if raw_args and raw_args != "null" else {}
                else:
                    args = raw_args or {}
                # Remove stray backslashes from invalid JSON escapes (e.g. \` or
                # \$ emitted by small local models) before any tool sees the
                # args. Provider-agnostic: this runs after provider parsing, so
                # it covers Ollama/Groq/OpenRouter alike. No-op when clean.
                args = strip_invalid_json_escapes(args)

                # Confirm-required calls are NOT executed here — the gate is
                # handled after this loop (so non-confirm tools, including
                # read_file which sets last_read_path, run first), and extras
                # were already auto-rejected above.
                if name in REQUIRES_CONFIRMATION:
                    continue

                yield _sse("tool_requested", json.dumps({"tool": name, "args": args}))

                # The web/search tools were consolidated into a single `web_tool`
                # (action: "search"|"fetch", arg: "target") and file listing +
                # semantic search into `search_workspace` (arg: "query"). The old
                # names (web_search/fetch_webpage/list_files/search_semantic) are
                # no longer emittable, so every branch below keys off the current
                # names. Both web_tool actions count toward the search budget,
                # matching the pre-consolidation behavior.
                is_web = name == "web_tool"
                is_web_search = is_web and args.get("action") == "search"
                is_web_fetch = is_web and args.get("action") == "fetch"

                if search_count >= max_searches and is_web:
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

                if tool_count >= max_tools:
                    yield _sse("status", f"Total tool cap ({max_tools}) reached...")
                    pending_tool_messages.append((tc.id, f"Total tool limit of {max_tools} reached. No more tools can be called. Please proceed to answer the user with the information you already have."))
                    continue

                event_payload = {"tool": name, "args": args}
                if is_web_search:
                    event_payload["count"] = search_count + 1
                    event_payload["max"] = max_searches
                    event_payload["label"] = f'searching: "{args.get("target", "")}" ({search_count + 1}/{max_searches})'
                elif is_web_fetch:
                    event_payload["label"] = f'fetching: {args.get("target", "")}'
                elif name == "search_workspace":
                    _q = args.get("query")
                    event_payload["label"] = (
                        f'searching workspace: "{_q}"' if _q else "listing workspace files"
                    )
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
                else:
                    event_payload["label"] = f'{name}: {json.dumps(args)}'

                yield _sse("searching", json.dumps(event_payload))
                # Cap args in the log: propose_edit/write args can carry an
                # entire file's contents, which would bloat the log file.
                _args_str = json.dumps(args)
                if len(_args_str) > 500:
                    _args_str = _args_str[:500] + f"...(+{len(_args_str) - 500} chars)"
                log.info("tool_call | %s | args=%s", name, _args_str)

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
                    # Track last read_file path for the propose_edit path-fill
                    # fallback (applied at the confirm gate, since propose_edit
                    # itself is gated and never executed here).
                    if name == "read_file" and args.get("path"):
                        last_read_path = args["path"]

                    _t0 = time.perf_counter()
                    try:
                        result = await mcp.call_tool(name, args)
                    except Exception as tool_exc:
                        _dt = (time.perf_counter() - _t0) * 1000
                        log.error(
                            "tool_error | %s | %.0fms | %s: %s",
                            name, _dt, type(tool_exc).__name__, tool_exc,
                        )
                        raise
                    _dt = (time.perf_counter() - _t0) * 1000
                    _result_str = result if isinstance(result, str) else str(result)
                    # Tool-reported failures come back as a normal string result
                    # (no exception), so sniff the leading text for error markers
                    # to flag them at WARNING instead of burying them in INFO.
                    _lead = _result_str.lstrip()[:40].lower()
                    _looks_failed = _lead.startswith(("error", "failed", "traceback", "[error"))
                    log.log(
                        logging.WARNING if _looks_failed else logging.INFO,
                        "tool_result | %s | %.0fms | %d chars | %r",
                        name, _dt, len(_result_str), _result_str[:300],
                    )

                if is_web_fetch:
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

                if use_remote_provider and is_web:
                    result = await _summarize_tool_result(resolved_provider, name, result)
                    yield _sse("status", f"summarized {name} result")

                if is_web:
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

            # ── Gate the turn on the first confirm-required call ─────────────
            # All non-confirm tools ran (results + last_read_path ready) and any
            # extra confirm calls were auto-rejected above. Now resolve the
            # preview for the gate call against the file AS IT IS NOW, emit the
            # confirm_required SSE, and RETURN — leaving gate_tc UNANSWERED in
            # the DB (no placeholder, no tool result). /respond resolves + resumes
            # from DB truth.
            if gate_tc is not None:
                raw_args = gate_tc.function.arguments
                if isinstance(raw_args, str):
                    gate_args = json.loads(raw_args) if raw_args and raw_args != "null" else {}
                else:
                    gate_args = raw_args or {}
                # Strip invalid JSON escapes (e.g. \` or \$) so both 'content'
                # (written to disk) and 'anchor' (matched against the file) are
                # byte-correct before preview/resolve. Done before the path-fill
                # and edit_inputs construction. Deterministic clean of incoming
                # args only — no mutation of prior history (KV-cache safe).
                gate_args = strip_invalid_json_escapes(gate_args)

                # Auto-fill missing 'path' from the last read_file, matching the
                # propose_edit tool's own fallback.
                if last_read_path and not gate_args.get("path"):
                    gate_args["path"] = last_read_path

                edit_inputs = {
                    "path": gate_args.get("path", ""),
                    "action": gate_args.get("action", "replace"),
                    "anchor": gate_args.get("anchor", ""),
                    "content": gate_args.get("content", ""),
                    "summary": gate_args.get("summary", ""),
                }

                # Surface the edit proposal in the chat transcript as an
                # expandable pill showing the full args it tried (action, path,
                # anchor, content). Confirm-required tools skip the normal
                # tool_requested emit in the batch loop above, so without this
                # the edit never shows in the UI chat flow (only the modal).
                yield _sse("edit_proposed", json.dumps({
                    "tool": gate_tc.function.name,
                    "path": edit_inputs["path"],
                    "action": edit_inputs["action"],
                    "anchor": edit_inputs["anchor"],
                    "content": edit_inputs["content"],
                    "summary": edit_inputs["summary"],
                }))

                preview = _resolve_preview(edit_inputs)

                # ── Auto-reject invalid edits before the UI sees them ─────────
                # If the edit can't be resolved against the file as-it-is-now
                # (anchor not found / not unique / file missing), don't bother
                # the user with a broken approval card. Instead answer the gate
                # tool call with the SPECIFIC error so the model can re-propose,
                # and continue the loop. Capped at MAX_INVALID_EDIT_RETRIES so a
                # model that can't land an anchor doesn't spin forever — past the
                # cap we fall through and surface the failed preview to the UI.
                if not preview.get("ok") and invalid_edit_retries < MAX_INVALID_EDIT_RETRIES:
                    invalid_edit_retries += 1
                    err = preview.get("error", "unknown error")
                    result_content = _invalid_edit_result(err, invalid_edit_retries, MAX_INVALID_EDIT_RETRIES)
                    conversation_history.append({
                        "role": "tool",
                        "tool_call_id": gate_tc.id,
                        "content": result_content,
                    })
                    db.add_message(conversation_id, "tool", result_content, tool_call_id=gate_tc.id)
                    # Distinct, visible pill so the user sees the edit is being
                    # auto-rejected and WHY (specific anchor error), and that the
                    # model is being asked to retry.
                    yield _sse("edit_rejected", json.dumps({
                        "path": edit_inputs["path"],
                        "action": edit_inputs["action"],
                        "error": err,
                        "attempt": invalid_edit_retries,
                        "max_attempts": MAX_INVALID_EDIT_RETRIES,
                    }))
                    continue

                confirm_payload = {
                    "toolCallId": gate_tc.id,
                    "conversation_id": conversation_id,
                    "tool": gate_tc.function.name,
                    "edit": {
                        "path": edit_inputs["path"],
                        "action": edit_inputs["action"],
                        "anchor": edit_inputs["anchor"],
                        "summary": edit_inputs["summary"],
                    },
                    "preview": preview,
                }
                yield _sse("confirm_required", json.dumps(confirm_payload))
                return
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


def _resolve_preview(edit_inputs: dict) -> dict:
    """Resolve an edit's inputs against the file AS IT IS NOW and return the
    contract ``preview`` shape. Never raises — a stale/invalid edit becomes
    ``{"ok": false, "error": "<msg>"}``. Used by the propose path, the resume
    path, and GET /pending_edit so all three render/apply against current state.

    On success: ``{"ok": true, "old_content", "new_content", "target_path"}``."""
    from tools.files import WORKSPACE_ROOT
    from tools.edit import resolve_edit

    edit = {
        "path": edit_inputs.get("path", ""),
        "action": edit_inputs.get("action", "replace"),
        "anchor": edit_inputs.get("anchor", ""),
        "content": edit_inputs.get("content", ""),
    }
    try:
        resolved = resolve_edit(WORKSPACE_ROOT, edit)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {
        "ok": True,
        "old_content": resolved["old_content"],
        "new_content": resolved["new_content"],
        "target_path": resolved["target_path"],
    }


async def agent_loop(user_message: str, mcp, model_override: str = "default", conversation_id: str = None, ollama_thinking: bool = False, attachments: list = None) -> AsyncGenerator[str, None]:
    # `attachments` is the whole attachment bucket from the request. Each item
    # is a `data:<mime>;base64,...` URL (or a {name, dataUrl} object) — images,
    # PDFs, and future doc types all flow here.
    import db
    import file_store
    try:
        if conversation_id:
            yield _sse("conversation_id", conversation_id)
        yield _sse("status", "loop started")
        clean_message = user_message.replace("<thinking>", "").replace("<coding>", "").strip()

        # Prepare any attached files (PDF → page images + text, images → one
        # page no text). prep_file renders/extracts ONCE and writes everything
        # to disk under chat_files/<conv>/<file_id>/; it returns a lean DB object
        # plus a page-labeled text block. The extracted text is spliced into THIS
        # turn's message content (and stored in the DB content) so it is pinned
        # to the upload turn and never re-injected later. Page images are fed to
        # the model this turn and re-attached deterministically on later turns.
        import prep_file as prep_file_mod

        prepared = []
        for item in attachments or []:
            name = None
            data_url = item
            if isinstance(item, dict):
                # Allow a richer FE payload {name, dataUrl} without breaking the
                # bare-string (data URL) contract.
                name = item.get("name")
                data_url = item.get("dataUrl") or item.get("data_url") or item.get("url")
            if not data_url:
                continue
            pf = prep_file_mod.prep_file(conversation_id, name or "attachment", data_url)
            if pf:
                prepared.append(pf)

        file_objs = [pf.db_object for pf in prepared]

        # Tell the UI about the freshly-persisted files so it can upgrade the
        # optimistic chips (shown at send time with no link) into real links to
        # the stored originals. Without this, a chip only becomes clickable after
        # a full conversation reload. Carries the conversation id so the frontend
        # can build /chat-file/<conv>/raw/<file_id>.<ext> URLs.
        if file_objs:
            yield _sse("files_prepared", json.dumps({
                "conversation_id": conversation_id,
                "files": file_objs,
            }))

        current_page_b64s: list[str] = []
        for pf in prepared:
            for rel in pf.page_rel_paths:
                b64 = file_store.load_as_base64(rel)
                if b64:
                    current_page_b64s.append(b64)

        # The DB `content` stores ONLY the user's typed text — that is what the
        # UI renders in the user bubble. The attachment's page-labeled text is
        # MODEL-ONLY context: it's appended to the message the model sees (below
        # and, for past turns, in _rehydrate_history_files), rebuilt from disk
        # each turn. This keeps the extracted PDF/doc text out of the visible
        # chat bubble while still feeding it to the model.
        display_content = clean_message

        # Load history and save the new user message (user text + file objects
        # only; NO base64 and NO attachment text in the DB).
        conversation_history = db.get_messages(conversation_id)
        db.add_message(conversation_id, "user", display_content, files=file_objs or None)

        user_msg = {"role": "user", "content": display_content}
        if file_objs:
            # Carry the file objects so _apply_file_context can fold in the
            # extracted text (and so the shape matches rehydrated history).
            user_msg["files"] = file_objs
            _apply_file_context(conversation_id, user_msg)
            user_msg.pop("files", None)
        if current_page_b64s:
            # Ollama /api/chat accepts a bare-base64 `images` list on a message
            # (same shape the OCR MoondreamBackend uses).
            user_msg["images"] = current_page_b64s
        conversation_history.append(user_msg)

        # Re-attach page images for OLDER user messages reloaded from history so
        # the model keeps full visual context across the whole conversation.
        # Extracted text is already in each message's content (pinned at upload),
        # so only the images are rebuilt here — deterministically, to keep the
        # KV-cache prefix byte-identical turn to turn.
        _rehydrate_history_files(conversation_id, conversation_history[:-1])
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

        tool_model, answer_model = _resolve_turn_models(
            resolved_provider, use_remote_provider, model_override, user_message
        )
        if use_remote_provider and answer_model != tool_model:
            yield _sse("model_upgrade", answer_model)

        log.info("agent_loop: starting")
        log.info("agent_loop: provider=%s | ollama_thinking=%s | model_override=%s", resolved_provider, ollama_thinking, model_override)
        mcp_tools = await mcp.list_tools()
        log.info("agent_loop: mcp_tools fetched")
        yield _sse("status", f"got {len(mcp_tools)} tools")

        ollama_tools = build_ollama_tools(mcp_tools)
        log.info("agent_loop: tools mapped")
        yield _sse("status", f"calling {resolved_provider}")

        async for chunk in _run_tool_loop(
            conversation_history,
            conversation_id,
            mcp,
            resolved_provider,
            use_remote_provider,
            ollama_tools,
            answer_model,
            tool_model,
            model_override,
            ollama_thinking,
        ):
            yield chunk


    except Exception as e:
        # Log one clear line naming the actual cause, then keep the full
        # traceback at debug level so it doesn't bury the real error.
        resp = getattr(e, "response", None)
        if resp is not None:
            try:
                body = resp.text
            except Exception:
                try:
                    body = resp.content.decode(errors="replace")
                except Exception:
                    body = "<unreadable body>"
            status = getattr(resp, "status_code", "?")
            log.error(
                "agent_loop FAILED: HTTP %s from provider — %s",
                status,
                (body or "<empty body>").strip()[:1000],
            )
        else:
            log.error("agent_loop FAILED: %s: %s", type(e).__name__, e)
        log.debug("agent_loop traceback:\n%s", traceback.format_exc())
        yield _sse("error", traceback.format_exc())
        yield _sse("done", "{}")


# ─── Break-and-resume: approve/reject a pending edit, then resume the turn ───

# Contract result strings (frozen — see design doc). `{path}`, `{action}`, and
# `{msg}` are filled per call; everything else is byte-for-byte fixed.
def _applied_result(path: str, action: str) -> str:
    return f"Applied: {path} ({action})."


def _stale_result(msg: str) -> str:
    return (
        "Edit NOT applied — the file changed since this edit was proposed and "
        "the anchor no longer matches uniquely. Error: "
        f"{msg}. Re-propose the edit against the current file contents."
    )


def _invalid_edit_result(msg: str, attempt: int, max_attempts: int) -> str:
    """Tool result for an edit whose anchor failed to resolve at the gate (auto
    -rejected server-side, never shown to the user). Carries the specific error
    so the model can fix the anchor and re-propose."""
    return (
        "Edit NOT applied — it could not be resolved against the current file "
        f"(attempt {attempt} of {max_attempts}). Error: {msg}. "
        "Fix the anchor (copy the exact text from the file, add surrounding "
        "context to make it unique) and call propose_edit again."
    )


def _reject_result(reason: str | None = None) -> str:
    """Tool result for a user-rejected edit. Includes the user's reason when
    provided so the model can act on it (e.g. adjust the approach) rather than
    just knowing the edit was declined."""
    base = "Edit rejected by the user. Do not re-apply it as-is."
    reason = (reason or "").strip()
    if reason:
        return f"{base} User's reason: {reason}. Take this into account and continue with the user's intent."
    return f"{base} Continue with the user's intent."


async def resume_after_confirmation(conversation_id: str, toolCallId: str, decision: str, mcp, reason: str | None = None) -> AsyncGenerator[str, None]:
    """Resolve a pending edit and resume the agent turn (break-and-resume).

    Looks up the dangling confirm-required tool call by `toolCallId`, applies the
    decision against the file AS IT IS NOW (recompute, never snapshot), appends
    the REAL tool result to the DB exactly once (answering the dangling call),
    then rebuilds history + system prompt + tools from DB truth and hands off to
    the shared `_run_tool_loop`. Because history is rebuilt deterministically and
    only one new tool message is appended, Ollama reuses its KV cache for the
    unchanged prefix.

    Mirrors `agent_loop`'s provider/model/tool setup so the resumed turn is
    indistinguishable from a normal turn to `_run_tool_loop`.
    """
    import db
    try:
        yield _sse("conversation_id", conversation_id)
        yield _sse("status", "resuming after confirmation")

        pending = db.get_pending_tool_call(conversation_id)
        if not pending or pending["tool_call_id"] != toolCallId:
            # The caller (/respond) already validated this, but double-check so a
            # race (resolved in another tab) can't double-apply.
            yield _sse("error", "No matching pending edit to resolve.")
            yield _sse("done", "{}")
            return

        raw_args = pending.get("arguments") or "{}"
        if isinstance(raw_args, str):
            try:
                edit_args = json.loads(raw_args) if raw_args and raw_args != "null" else {}
            except json.JSONDecodeError:
                edit_args = {}
        else:
            edit_args = raw_args or {}
        # Sanitize invalid JSON escapes here too: this is the path that actually
        # WRITES the file on approve (args are read fresh from the DB, which may
        # hold the raw, pre-sanitize stored arguments), so both the preview and
        # the on-disk result stay byte-correct.
        from tools.edit import strip_invalid_json_escapes
        edit_args = strip_invalid_json_escapes(edit_args)

        edit_inputs = {
            "path": edit_args.get("path", ""),
            "action": edit_args.get("action", "replace"),
            "anchor": edit_args.get("anchor", ""),
            "content": edit_args.get("content", ""),
            "summary": edit_args.get("summary", ""),
        }

        if decision == "approve":
            preview = _resolve_preview(edit_inputs)
            if preview.get("ok"):
                # Apply: write the recomputed new_content to the resolved target.
                target = Path(preview["target_path"])
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("w", encoding="utf-8") as f:
                    f.write(preview["new_content"])
                result_content = _applied_result(edit_inputs["path"], edit_inputs["action"])
                yield _sse("status", "edit applied")
            else:
                # Stale anchor / file changed — do NOT write; let the model
                # re-propose against current contents.
                result_content = _stale_result(preview.get("error", "unknown error"))
                yield _sse("status", "edit could not be applied (stale)")
        else:
            # reject (any non-approve decision is treated as a rejection).
            # The user's reason (if any) is folded into the tool result so the
            # model learns WHY and can adjust rather than blindly retrying.
            result_content = _reject_result(reason)
            yield _sse("status", "edit rejected")

        # Append the REAL tool result exactly once — this answers the dangling
        # confirm tool call and makes the conversation no longer "pending".
        db.add_message(conversation_id, "tool", result_content, tool_call_id=toolCallId)

        # Rebuild history from DB truth (deterministic) and re-attach page images
        # for older user messages so the model keeps full visual context — same
        # rebuild the live path uses, keeping the KV-cache prefix byte-identical.
        conversation_history = db.get_messages(conversation_id)
        _rehydrate_history_files(conversation_id, conversation_history)
        yield _sse("status", "history rebuilt")

        # Provider / model / tool setup — mirror agent_loop.
        resolved_provider = db.resolve_conversation_provider(
            conversation_id,
            llm_provider.get_effective_provider(CONFIG)
        )
        use_remote_provider = resolved_provider in {"groq", "openrouter"}
        # No user_message to re-route on at resume time; model selection for the
        # remote path keyword-routes on the last user turn's text if available.
        last_user_text = ""
        for m in reversed(conversation_history):
            if m.get("role") == "user" and m.get("content"):
                last_user_text = m["content"]
                break
        tool_model, answer_model = _resolve_turn_models(
            resolved_provider, use_remote_provider, "default", last_user_text
        )

        mcp_tools = await mcp.list_tools()
        ollama_tools = build_ollama_tools(mcp_tools)
        yield _sse("status", f"calling {resolved_provider}")

        async for chunk in _run_tool_loop(
            conversation_history,
            conversation_id,
            mcp,
            resolved_provider,
            use_remote_provider,
            ollama_tools,
            answer_model,
            tool_model,
            "default",
            False,
        ):
            yield chunk

    except Exception as e:
        resp = getattr(e, "response", None)
        if resp is not None:
            try:
                body = resp.text
            except Exception:
                try:
                    body = resp.content.decode(errors="replace")
                except Exception:
                    body = "<unreadable body>"
            status = getattr(resp, "status_code", "?")
            log.error("resume_after_confirmation FAILED: HTTP %s — %s", status, (body or "<empty>").strip()[:1000])
        else:
            log.error("resume_after_confirmation FAILED: %s: %s", type(e).__name__, e)
        log.debug("resume traceback:\n%s", traceback.format_exc())
        yield _sse("error", traceback.format_exc())
        yield _sse("done", "{}")
