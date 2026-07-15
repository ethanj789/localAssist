"""
Skill executor — runs skill steps sequentially, appends results to conversation
history, streams SSE events for live UI feedback, and optionally asks the LLM
to reason between steps.

The executor reuses the same SSE format as agent.py so the frontend can handle
skill execution with the same stream handler.
"""

import asyncio
import json
import logging
from datetime import date, datetime
from typing import AsyncGenerator

import db
import llm_provider
import prompts
from config import CONFIG, get_model_setting, load_memory

log = logging.getLogger(__name__)


def _sse(event: str, data: str) -> str:
    """Format an SSE line — same format as agent.py."""
    payload = json.dumps({"event": event, "data": data})
    return f"data: {payload}\n\n"


def _interpolate(template: str, context: dict) -> str:
    """Simple {key} variable substitution in a string."""
    result = template
    for key, value in context.items():
        result = result.replace("{" + key + "}", str(value))
    return result


def _interpolate_args(args: dict, context: dict) -> dict:
    """Interpolate all string values in an args dict."""
    return {k: _interpolate(v, context) if isinstance(v, str) else v for k, v in args.items()}


def _parse_skill_inputs(skill: dict, raw_args: str) -> dict:
    """
    Parse user-provided arguments for a skill.
    
    Simple strategy: if there's one required input, the entire raw_args string
    is its value. If multiple, split by spaces (first N-1 are positional, last
    gets the remainder). Falls back to defaults for missing optional inputs.
    """
    inputs = skill.get("inputs", [])
    if not inputs:
        return {}

    result = {}
    parts = raw_args.strip().split() if raw_args.strip() else []

    required = [i for i in inputs if i.get("required", False)]
    optional = [i for i in inputs if not i.get("required", False)]

    if len(required) == 1 and not optional:
        # Single required arg: whole string is its value
        result[required[0]["name"]] = raw_args.strip()
    elif len(required) <= 1 and len(inputs) == 1:
        # Single input total
        result[inputs[0]["name"]] = raw_args.strip() if raw_args.strip() else inputs[0].get("default", "")
    else:
        # Positional assignment
        all_inputs = required + optional
        for i, inp in enumerate(all_inputs):
            if i < len(parts) - 1:
                result[inp["name"]] = parts[i]
            elif i == len(all_inputs) - 1 and parts:
                # Last arg gets remainder
                result[inp["name"]] = " ".join(parts[i:])
            else:
                result[inp["name"]] = inp.get("default", "")

    # Fill defaults for anything missing
    for inp in inputs:
        if inp["name"] not in result or not result[inp["name"]]:
            result[inp["name"]] = inp.get("default", "")

    return result


async def run_skill(
    skill: dict,
    raw_args: str,
    mcp,
    conversation_id: str,
) -> AsyncGenerator[str, None]:
    """
    Execute a skill's steps within a conversation.
    
    Yields SSE events for live streaming to the frontend.
    Appends all results to conversation history (DB + returned for agent context).
    """
    skill_name = skill["name"]
    think_between = skill.get("think_between_steps", False)

    # Parse inputs
    user_inputs = _parse_skill_inputs(skill, raw_args)
    
    # Build interpolation context with built-in variables
    context = {
        "date": date.today().isoformat(),
        "time": datetime.now().strftime("%H:%M"),
        "day": datetime.now().strftime("%A"),
        **user_inputs,
    }

    yield _sse("skill_start", json.dumps({
        "name": skill_name,
        "command": skill["command"],
        "inputs": user_inputs,
        "total_steps": len(skill["steps"]),
    }))

    log.info("skill_executor: starting '%s' with inputs=%s", skill_name, user_inputs)

    # We'll collect all step results to build the final aggregated message
    step_results: list[dict] = []
    conversation_history = db.get_messages(conversation_id)

    # Add the user message (the /command) to history
    user_msg = f"{skill['command']} {raw_args}".strip()
    db.add_message(conversation_id, "user", user_msg)
    conversation_history.append({"role": "user", "content": user_msg})

    for i, step in enumerate(skill["steps"]):
        step_id = step.get("id", f"step_{i}")
        action = step["action"]

        yield _sse("skill_step", json.dumps({
            "step_index": i,
            "step_id": step_id,
            "total_steps": len(skill["steps"]),
            "action": action,
            "status": "running",
        }))

        if action == "tool":
            tool_name = step["tool"]
            tool_args = _interpolate_args(step.get("args", {}), context)

            yield _sse("searching", json.dumps({
                "tool": tool_name,
                "args": tool_args,
                "label": f"skill step {i+1}/{len(skill['steps'])}: {tool_name} — {json.dumps(tool_args)}",
            }))

            try:
                result = await mcp.call_tool(tool_name, tool_args)
                log.info("skill step %s/%s done: %s | result_len=%d", i+1, len(skill["steps"]), tool_name, len(result))
            except Exception as e:
                result = f"Error calling {tool_name}: {e}"
                log.error("skill step %s/%s failed: %s | %s", i+1, len(skill["steps"]), tool_name, e)

            # Store in context for future step interpolation
            if step.get("output_key"):
                context[step["output_key"]] = result

            step_results.append({"step_id": step_id, "tool": tool_name, "args": tool_args, "result": result})

            yield _sse("skill_step", json.dumps({
                "step_index": i,
                "step_id": step_id,
                "total_steps": len(skill["steps"]),
                "action": action,
                "status": "done",
            }))

            yield _sse("search_result", json.dumps({"tool": tool_name, "count": i + 1}))

        elif action == "llm":
            # Mid-step LLM reasoning — appends to conversation, KV stays warm
            llm_prompt = _interpolate(step.get("prompt", ""), context)
            
            # Append step results so far as a tool message for context
            yield _sse("skill_step", json.dumps({
                "step_index": i,
                "step_id": step_id,
                "total_steps": len(skill["steps"]),
                "action": "llm",
                "status": "thinking",
            }))

            llm_result = await _call_llm_for_step(llm_prompt, conversation_history, conversation_id)
            
            if step.get("output_key"):
                context[step["output_key"]] = llm_result

            step_results.append({"step_id": step_id, "action": "llm", "result": llm_result})

            yield _sse("skill_step", json.dumps({
                "step_index": i,
                "step_id": step_id,
                "total_steps": len(skill["steps"]),
                "action": "llm",
                "status": "done",
            }))

        # Optional mid-step thinking (LLM reasons about what happened)
        if think_between and i < len(skill["steps"]) - 1 and action == "tool":
            think_prompt = (
                f"Skill '{skill_name}' step {i+1}/{len(skill['steps'])} completed.\n"
                f"Tool: {step.get('tool', 'llm')} | Result length: {len(step_results[-1]['result'])} chars.\n"
                f"Briefly note anything interesting or relevant for the next steps."
            )
            thinking_result = await _call_llm_for_step(think_prompt, conversation_history, conversation_id)
            yield _sse("thinking_token", thinking_result)

    # All steps done — build the final aggregated tool result message
    aggregated_content = _build_aggregated_result(skill, step_results, context)
    
    # Append as a tool-like message so the LLM sees it in context
    # Use a synthetic tool_call_id for the conversation flow
    tool_call_id = f"skill_{skill['command'].strip('/')}_{conversation_id[-8:]}"
    
    # Add an assistant message with a synthetic tool_call to maintain proper message ordering
    skill_tool_call = [{
        "id": tool_call_id,
        "type": "function",
        "function": {
            "name": f"skill:{skill['command'].strip('/')}",
            "arguments": json.dumps(user_inputs),
        }
    }]
    conversation_history.append({"role": "assistant", "content": None, "tool_calls": skill_tool_call})
    db.add_message(conversation_id, "assistant", None, tool_calls=skill_tool_call)

    # Add the aggregated result as a tool response
    conversation_history.append({
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": aggregated_content,
    })
    db.add_message(conversation_id, "tool", aggregated_content, tool_call_id=tool_call_id)

    yield _sse("skill_complete", json.dumps({
        "name": skill_name,
        "steps_completed": len(skill["steps"]),
    }))

    # Now let the LLM respond naturally with the aggregated context
    log.info("skill_executor: running final LLM pass for '%s'", skill_name)
    async for chunk in _final_llm_response(conversation_history, conversation_id):
        yield chunk


async def _call_llm_for_step(prompt: str, conversation_history: list, conversation_id: str) -> str:
    """
    Make a mid-step LLM call that appends to conversation history (KV-friendly).
    Returns the assistant's response text.
    """
    # Append the prompt as a user message (will be part of ongoing context)
    conversation_history.append({"role": "user", "content": prompt})
    db.add_message(conversation_id, "user", prompt)

    provider = llm_provider.get_effective_provider(CONFIG)
    settings = get_model_setting("answer", "default")

    memory = load_memory()
    system = prompts.SYSTEM_PROMPT
    if memory:
        system += "\n\n" + memory

    request_kwargs = {
        "model": settings.get("model", CONFIG["model"]) if provider != "ollama" else CONFIG["model"],
        "messages": [
            {"role": "system", "content": system},
            *conversation_history[-10:],  # Keep it bounded
        ],
        "temperature": settings.get("temperature", CONFIG["temperature"]),
        "max_completion_tokens": settings.get("max_tokens", 500),
    }

    if provider != "ollama":
        reasoning_effort = settings.get("reasoning_effort")
        if isinstance(reasoning_effort, str) and reasoning_effort.strip():
            request_kwargs["reasoning_effort"] = reasoning_effort.strip()

    try:
        if provider == "ollama":
            # Use non-streaming for mid-step (simpler, still appends to KV)
            response = await llm_provider._ollama_chat_completion(request_kwargs, CONFIG)
            content = response.choices[0].message.content or ""
        else:
            response = await llm_provider.chat_completion_with_retry(provider, request_kwargs, CONFIG)
            content = response.choices[0].message.content or ""
    except Exception as e:
        log.error("Mid-step LLM call failed: %s", e)
        content = f"(thinking failed: {e})"

    # Append assistant response to history
    conversation_history.append({"role": "assistant", "content": content})
    db.add_message(conversation_id, "assistant", content)

    return content


async def _final_llm_response(conversation_history: list, conversation_id: str) -> AsyncGenerator[str, None]:
    """
    Final LLM call after all skill steps complete.
    Streams tokens to the UI, appends to conversation.
    """
    provider = llm_provider.get_effective_provider(CONFIG)
    settings = get_model_setting("answer", "default")

    memory = load_memory()
    system = prompts.SYSTEM_PROMPT
    if memory:
        system += "\n\n" + memory

    request_kwargs = {
        "model": settings.get("model", CONFIG["model"]) if provider != "ollama" else CONFIG["model"],
        "messages": [
            {"role": "system", "content": system},
            *conversation_history[-15:],
        ],
        "temperature": settings.get("temperature", CONFIG["temperature"]),
        "max_completion_tokens": settings.get("max_tokens", CONFIG["max_tokens"]),
    }

    if provider != "ollama":
        reasoning_effort = settings.get("reasoning_effort")
        if isinstance(reasoning_effort, str) and reasoning_effort.strip():
            request_kwargs["reasoning_effort"] = reasoning_effort.strip()

    try:
        if provider == "ollama":
            full_content = ""
            async for stream_chunk in llm_provider._ollama_stream_chat_completion(request_kwargs, CONFIG):
                choice = stream_chunk.choices[0]
                delta = choice.delta
                if delta.thinking:
                    yield _sse("thinking_token", delta.thinking)
                if delta.content:
                    yield _sse("token", delta.content)
                    full_content += delta.content
                if choice.finish_reason is not None:
                    assembled = stream_chunk.assembled
                    if assembled and assembled.verbose_stats:
                        yield _sse("ollama_stats", json.dumps(assembled.verbose_stats))
                    break
            content = full_content
        else:
            response = await llm_provider.chat_completion_with_retry(provider, request_kwargs, CONFIG)
            content = response.choices[0].message.content or ""
            # Stream word-by-word for remote providers
            for word in content.split(" "):
                yield _sse("token", word + " ")
                await asyncio.sleep(0.01)

    except Exception as e:
        log.error("Final LLM response failed: %s", e)
        content = f"Skill completed but final response failed: {e}"
        yield _sse("token", content)

    # Persist
    if content:
        conversation_history.append({"role": "assistant", "content": content})
        db.add_message(conversation_id, "assistant", content)

    yield _sse("done", json.dumps({
        "searches_used": 0,
        "emails_used": 0,
        "tools_used": len([s for s in conversation_history if s.get("role") == "tool"]),
        "max_searches": CONFIG["max_searches"],
        "max_emails": CONFIG["max_emails"],
        "max_tools": CONFIG["max_tools"],
    }))


def _build_aggregated_result(skill: dict, step_results: list[dict], context: dict) -> str:
    """Build the aggregated content from all step results + the result_prompt."""
    parts = []
    
    # Include the result prompt if defined
    result_prompt = skill.get("result_prompt", "")
    if result_prompt:
        parts.append(_interpolate(result_prompt, context))
        parts.append("")

    # Append each step's result
    for sr in step_results:
        if sr.get("tool"):
            parts.append(f"--- [{sr['step_id']}] {sr['tool']}({json.dumps(sr['args'])}) ---")
        else:
            parts.append(f"--- [{sr['step_id']}] LLM reasoning ---")
        parts.append(sr.get("result", "(no result)"))
        parts.append("")

    return "\n".join(parts)
