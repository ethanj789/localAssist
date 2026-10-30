import asyncio
import json
import time
import logging
from types import SimpleNamespace
from typing import AsyncGenerator

from config import CONFIG, MAX_HISTORY, get_model_setting
import llm_provider
import plans

from .sse import _sse
from .prompt import build_system_prompt
from .parsing import _extract_tool_call_from_text
from .history import _prune_history, _maybe_summarize_history, _summarize_tool_result
from .edits import (
    _resolve_preview,
    _invalid_edit_result,
    _read_only_edit_result,
    REQUIRES_CONFIRMATION,
    AUTO_REJECT_EXTRA_EDIT,
)

log = logging.getLogger(__name__)


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
    # PER-EDIT retry budget for edits whose anchor didn't resolve at the gate.
    # Each failing edit is auto-rejected server-side with the specific error (so
    # the model can re-propose) WITHOUT bothering the UI. The counter tracks
    # retries for the CURRENT edit target and resets whenever the model moves on
    # to a different edit (different path+anchor), so a long run of successful
    # edits is never starved just because earlier edits each needed a retry.
    # The cap only stops a model stuck re-proposing the SAME unresolvable anchor.
    invalid_edit_retries = 0
    last_failed_edit_key: tuple[str, str] | None = None
    MAX_INVALID_EDIT_RETRIES = 3
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

            # Persist the reasoning emitted alongside this tool-call turn so it
            # survives a reload. The thinking tokens were already streamed live
            # (thinking_token SSE) but were previously dropped on the tool-call
            # branch, so the 💭 block vanished when the chat was reopened. We do
            # NOT inject thinking_content back into conversation_history (KV-cache
            # discipline: history stays byte-identical to what was sent to the
            # model); it's stored only for UI rebuild.
            tool_turn_thinking = getattr(msg, 'thinking', None) or None
            conversation_history.append({"role": "assistant", "content": None, "tool_calls": tool_calls_for_history})
            db.add_message(conversation_id, "assistant", None, tool_calls=tool_calls_for_history, thinking_content=tool_turn_thinking)

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

                # Emit the result text live so a result pill shows during
                # streaming (previously results only appeared after reopening the
                # chat, since they were just persisted as role:"tool" rows). The
                # reload path renders these from the stored tool rows via
                # appendToolResultPill — this mirrors that live. `plan` results
                # are internal status JSON and drive the plan_update UI instead,
                # so they're excluded from the transcript pill.
                if name != "plan":
                    _result_str = result if isinstance(result, str) else str(result)
                    yield _sse("tool_result", json.dumps({"tool": name, "result": _result_str}))

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

                # ── Read-only path (policy denial) — terminal, never retried ──
                # A notesAppText/ (or other read-only) target can never be made
                # writable by fixing the anchor/content, so don't count it as an
                # anchor retry or show a broken approval card. Answer the tool
                # call with the specific reason and continue.
                if not preview.get("ok") and preview.get("read_only"):
                    err = preview.get("error", "this path is read-only.")
                    result_content = _read_only_edit_result(err)
                    conversation_history.append({
                        "role": "tool",
                        "tool_call_id": gate_tc.id,
                        "content": result_content,
                    })
                    db.add_message(conversation_id, "tool", result_content, tool_call_id=gate_tc.id)
                    yield _sse("edit_rejected", json.dumps({
                        "path": edit_inputs["path"],
                        "action": edit_inputs["action"],
                        "error": err,
                        "read_only": True,
                    }))
                    continue

                # ── Auto-reject invalid edits before the UI sees them ─────────
                # If the edit can't be resolved against the file as-it-is-now
                # (anchor not found / not unique / file missing), don't bother
                # the user with a broken approval card. Instead answer the gate
                # tool call with the SPECIFIC error so the model can re-propose,
                # and continue the loop. The retry budget is PER EDIT: a new
                # target (different path+anchor) resets the counter, so only a
                # model stuck re-proposing the SAME unresolvable anchor is capped
                # — past the cap we surface the failed preview to the UI.
                if not preview.get("ok"):
                    edit_key = (edit_inputs["path"], edit_inputs["anchor"])
                    if edit_key != last_failed_edit_key:
                        # Model moved on to a different edit — fresh budget.
                        invalid_edit_retries = 0
                        last_failed_edit_key = edit_key

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
