"""
agent/subagents/core.py — shared bounded loop for a Granite worker subagent.

Runs `ibm/granite4:350m-h` and returns a single digest string (the only thing that
re-enters gemma's conversation). 

"""
import json
import logging
from types import SimpleNamespace

import llm_provider
from config import CONFIG

log = logging.getLogger(__name__)

WORKER_MODEL = "ibm/granite4:350m-h"
WORKER_TEMPERATURE = 0.3
WORKER_MAX_TOKENS = 512
WORKER_NUM_CTX = 8192


async def _noop_emit(event_type: str, data: dict) -> None:
    return None


async def _call_worker(
    messages: list[dict],
    tools: list[dict],
    *,
    agent_name: str,
    emit,
) -> SimpleNamespace:
    # model= overrides per-call without touching CONFIG["model"] (gemma). Never
    # send think:true to granite — it 400s. Returns a SimpleNamespace
    # (content, tool_calls, thinking); tc.function.arguments is a JSON string.
    request_kwargs = {
        "model": WORKER_MODEL,
        "messages": messages,
        "tools": tools,
        "temperature": WORKER_TEMPERATURE,
        "max_completion_tokens": WORKER_MAX_TOKENS,
        "num_ctx": WORKER_NUM_CTX,
    }

    assembled = None
    async for chunk in llm_provider._ollama_stream_chat_completion(request_kwargs, CONFIG):
        choice = chunk.choices[0]
        delta = choice.delta
        if delta.content:
            await emit("subagent_token", {"agent": agent_name, "token": delta.content})
        if choice.finish_reason is not None:
            assembled = chunk.assembled
            break

    if assembled is None:
        # Stream ended with no done chunk (rare): recover with a blocking call so
        # a round is never silently lost.
        log.warning("subagent %s | stream ended with no done chunk — falling back", agent_name)
        resp = await llm_provider._ollama_chat_completion(request_kwargs, CONFIG)
        return resp.choices[0].message

    content = assembled.content or ""
    thinking = assembled.thinking
    tool_calls = llm_provider._parse_ollama_tool_calls_raw(assembled.tool_calls_raw)

    if not tool_calls and not content:
        log.warning(
            "subagent %s | stream yielded no content/tool_calls — "
            "falling back to non-streaming call to recover",
            agent_name,
        )
        resp = await llm_provider._ollama_chat_completion(request_kwargs, CONFIG)
        return resp.choices[0].message

    return SimpleNamespace(
        content=content,
        tool_calls=tool_calls,
        thinking=thinking,
    )


def _parse_args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw or raw == "null":
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _tool_names(msg) -> list[str]:
    return [getattr(tc.function, "name", "?") for tc in (msg.tool_calls or [])]


async def run_subagent_loop(
    *,
    agent_name: str,
    task: str,
    system_prompt: str,
    tools_schema: list[dict],
    tool_runners: dict,
    max_rounds: int,
    max_tool_calls: int,
    emit=None,
) -> dict:
    """Run one bounded granite loop. `tool_runners` maps name -> async
    callable(args) -> str, each wrapping an in-process tool. Returns
    {"digest": str (never empty), "transcript": list, "ok": bool}."""
    if emit is None:
        emit = _noop_emit

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task},
    ]
    # Transcript carries explicit tool events for a future UI replay; kept apart
    # from `messages` so the provider-facing array isn't coupled to display shape.
    transcript: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task},
    ]

    await emit("subagent_start", {"agent": agent_name, "task": task})

    tool_calls_made = 0
    digest = None
    ok = False

    try:
        for rnd in range(max_rounds):
            # On the final round (or once the tool budget is spent) withhold the
            # tools so granite must write a digest instead of attempting another
            # call — turns a cap-hit into a real answer rather than an empty loop.
            budget_exhausted = tool_calls_made >= max_tool_calls
            at_last_round = rnd == max_rounds - 1
            offer_tools = not (budget_exhausted or at_last_round)

            msg = await _call_worker(
                messages,
                tools_schema if offer_tools else [],
                agent_name=agent_name,
                emit=emit,
            )
            content = (msg.content or "").strip()
            tool_calls = msg.tool_calls or []

            log.info(
                "subagent %s | round %d/%d | tool_calls=%s | content_chars=%d%s",
                agent_name, rnd + 1, max_rounds, _tool_names(msg), len(content),
                "" if offer_tools else " (tools withheld — forcing digest)",
            )

            # Terminator: no tool calls = done.
            if not tool_calls:
                digest = content
                ok = True
                transcript.append({"role": "assistant", "content": content})
                await emit("subagent_digest", {"agent": agent_name, "digest": content})
                break

            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments
                            if tc.function.arguments and tc.function.arguments != "null" else "{}",
                        },
                    }
                    for tc in tool_calls
                ],
            })

            for tc in tool_calls:
                name = getattr(tc.function, "name", "") or ""
                args = _parse_args(tc.function.arguments)

                if tool_calls_made >= max_tool_calls:
                    result = (
                        f"Tool budget reached ({max_tool_calls} calls). Stop calling "
                        "tools and write your concise final digest now."
                    )
                elif name not in tool_runners:
                    # Whitelist enforcement doubles as the recursion guard — a
                    # subagent can only ever reach its own listed tools.
                    result = f"Tool '{name}' is not available. Use only: {', '.join(tool_runners)}."
                else:
                    await emit("subagent_tool", {"agent": agent_name, "tool": name, "args": args})
                    try:
                        result = await tool_runners[name](args)
                    except Exception as tool_exc:
                        log.warning("subagent %s | tool %s raised: %s", agent_name, name, tool_exc)
                        result = f"Tool '{name}' error: {tool_exc}"
                    tool_calls_made += 1
                    await emit("subagent_tool_result", {"agent": agent_name, "tool": name, "result": result})

                if not isinstance(result, str):
                    result = str(result)

                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
                transcript.append({
                    "role": "tool",
                    "tool": name,
                    "args": args,
                    "content": result,
                })
    except Exception as e:
        log.error("subagent %s | loop failed: %s", agent_name, e, exc_info=True)
        ok = False
        digest = None

    # Never return empty — an empty tool result corrupts gemma's history. A real
    # failure yields a non-empty digest telling gemma it failed and to retry;
    # an honest "found nothing" keeps granite's own words.
    digest = (digest or "").strip()
    if not digest:
        ok = False
        digest = (
            f"Subagent {agent_name} failed to complete (no usable output). "
            "Retry the call or proceed without it."
        )
        transcript.append({"role": "assistant", "content": digest, "failure": True})
        await emit("subagent_failed", {"agent": agent_name, "reason": "empty digest"})

    return {"digest": digest, "transcript": transcript, "ok": ok}
