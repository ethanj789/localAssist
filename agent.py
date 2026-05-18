from groq.types.chat import chat_completion_system_message_param
import asyncio
import json
import traceback
import logging
from typing import AsyncGenerator
import groq
import httpx
import os
from config import (
    CONFIG, MAX_HISTORY, SUMMARIZE_THRESHOLD, SUMMARIZE_KEEP_LAST,
    CODING_MODEL, SUMMARY_MODEL, THINKING_MODEL, load_memory, select_groq_model
)

log = logging.getLogger(__name__)

conversation_history: list[dict] = []
global_pending_edit = None
global_edit_log: list[str] = []


def _sse(event: str, data: str) -> str:
    payload = json.dumps({"event": event, "data": data})
    return f"data: {payload}\n\n"


def _prune_history(history: list[dict], keep_last_n_tool_results: int = 2) -> list[dict]:
    pruned = []
    tool_result_count = 0
    for msg in reversed(history):
        if msg["role"] == "tool":
            # EXEMPT pending edits from pruning
            if msg.get("content") == "edit pending approval":
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


async def _maybe_summarize_history(groq_client) -> None:
    if len(conversation_history) < SUMMARIZE_THRESHOLD:
        return

    to_summarize = conversation_history[:-SUMMARIZE_KEEP_LAST]
    keep = conversation_history[-SUMMARIZE_KEEP_LAST:]
    summarizable = [m for m in to_summarize if m["role"] in ("user", "assistant") and m.get("content")]

    if not summarizable:
        return

    summary_response = await groq_client.chat.completions.create(
        model=SUMMARY_MODEL,
        messages=[
            {"role": "system", "content": "Summarize the following conversation history concisely in 3-5 sentences, preserving key facts and context."},
            {"role": "user", "content": json.dumps(summarizable)}
        ],
        max_completion_tokens=300,
        temperature=0.3,
    )

    summary = summary_response.choices[0].message.content
    conversation_history.clear()
    conversation_history.append({"role": "assistant", "content": f"[Previous conversation summary]: {summary}"})
    conversation_history.extend(keep)
    log.info("history summarized | new length=%d", len(conversation_history))


async def _summarize_tool_result(groq_client, tool_name: str, result: str) -> str:
    if len(result) < 300 or tool_name not in ("web_search", "fetch_webpage"):
        return result
    try:
        summary = await groq_client.chat.completions.create(
            model=SUMMARY_MODEL,
            messages=[
                {"role": "system", "content": (
                    "Summarize this search result in 3-4 sentences. "
                    "Preserve ALL URLs exactly as they appear — never paraphrase or omit them. "
                    "Preserve all key facts, numbers, dates, and names. "
                    "Be concise but don't lose important details." 
                )},
                {"role": "user", "content": result}
            ],
            max_completion_tokens=200,
            temperature=0.1,
        )
        summarized = summary.choices[0].message.content
        log.info("summarized tool result | %s | %d→%d chars", tool_name, len(result), len(summarized))
        return summarized
    except Exception as e:
        log.warning("summary failed, using raw result | %s", e)
        return result


async def agent_loop(user_message: str, mcp, model_override: str = "default") -> AsyncGenerator[str, None]:
    global global_pending_edit
    try:
        yield _sse("status", "loop started")
        clean_message = user_message.replace("<thinking>", "").replace("<coding>", "").strip()
        conversation_history.append({"role": "user", "content": clean_message})
        yield _sse("status", "history appended")

        if model_override == "coding":
            tool_model, answer_model = CONFIG["groq_model"], CODING_MODEL
        elif model_override == "thinking":
            tool_model, answer_model = CONFIG["groq_model"], THINKING_MODEL
        else:
            tool_model, answer_model = select_groq_model(user_message)

        if answer_model != CONFIG["groq_model"]:
            yield _sse("model_upgrade", answer_model)

        mcp_tools = await mcp.list_tools()
        yield _sse("status", f"got {len(mcp_tools)} tools")

        ollama_tools = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["inputSchema"],
                }
            }
            for t in mcp_tools
        ]
        yield _sse("status", f"calling {'groq' if CONFIG['use_groq'] else 'ollama'}")

        search_count = 0
        email_count = 0
        memory_count = 0
        tool_count = 0
        max_searches = CONFIG["max_searches"]
        max_emails = CONFIG["max_emails"]
        max_tools = CONFIG["max_tools"]
        max_memory = 3

        async with httpx.AsyncClient(timeout=240) as client:
            while True:
                try:
                    trimmed_history = _prune_history(conversation_history[-MAX_HISTORY:])
                    is_final_call = len(trimmed_history) > 0 and trimmed_history[-1]["role"] == "tool"
                    # current_model = answer_model if is_final_call else tool_model
                    current_model = answer_model # i give up trying to optimize this single call.

                    groq_client = None
                    if CONFIG["use_groq"]:
                        from groq import AsyncGroq
                        # groq_client = AsyncGroq(api_key=CONFIG["groq_api_key"])
                        groq_client = AsyncGroq(api_key=os.getenv("GROQ_API_KEY", ""))
                        await _maybe_summarize_history(groq_client)

                    memory = load_memory()
                    system = CONFIG["system_prompt"] + "\n\n" + CONFIG["memory_management_prompt"]
                    if memory:
                        system = system + "\n\n" + memory
                        
                    if global_edit_log:
                        system += "\n\nEdit Log:\n" + "\n".join(global_edit_log)

                    if CONFIG["use_groq"]:
                        try: 
                            yield _sse("model", json.dumps({
                                "provider": "groq",
                                "model": current_model
                            }))

                            # Filter tools based on budget and pending state
                            available_tools = []
                            
                            # Check if the immediately preceding message is the pending edit
                            last_was_pending_edit = len(trimmed_history) > 0 and trimmed_history[-1].get("role") == "tool" and trimmed_history[-1].get("content") == "edit pending approval"
                            
                            if last_was_pending_edit:
                                # Force prose response by stripping all tools
                                available_tools = []
                            else:
                                for t in ollama_tools:
                                    if tool_count >= max_tools:
                                        break
                                    if t["function"]["name"] in ("web_search", "fetch_webpage") and search_count >= max_searches:
                                        continue
                                    if t["function"]["name"] == "draft_email" and email_count >= max_emails:
                                        continue
                                    if t["function"]["name"] == "manage_memory" and memory_count >= max_memory:
                                        continue
                                    if global_pending_edit and t["function"]["name"] == "propose_edit":
                                        continue
                                    available_tools.append(t)

                            response = await groq_client.chat.completions.create(
                                model=current_model,
                                messages=[
                                    {"role": "system", "content": system},
                                    *trimmed_history,
                                ],
                                tools=available_tools if available_tools else [],
                                temperature=CONFIG["temperature"],
                                max_completion_tokens=CONFIG["max_tokens"],
                            )
                        except Exception as e:
                            if hasattr(e, 'response'):
                                log.error("Groq 400 body: %s", e.response.text)
                            raise
                        msg = response.choices[0].message
                        tool_calls = msg.tool_calls or []

                        content = msg.content
                        if tool_calls:
                            content = None
                        else:
                            content = content or ""                        
                        
                        log.info("groq response | content=%s | tool_calls=%s", content, tool_calls)
                        yield _sse("status", "groq responded")
                    else:
                        yield _sse("model", json.dumps({
                            "provider": "ollama",
                            "model": CONFIG["model"]
                        }))
                        # Filter tools based on budget for Ollama as well
                        available_tools = []
                        last_was_pending_edit = len(trimmed_history) > 0 and trimmed_history[-1].get("role") == "tool" and trimmed_history[-1].get("content") == "edit pending approval"
                        
                        if last_was_pending_edit:
                            available_tools = []
                        else:
                            for t in ollama_tools:
                                if tool_count >= max_tools:
                                    break
                                if t["type"] == "function" and t["function"]["name"] in ("web_search", "fetch_webpage") and search_count >= max_searches:
                                    continue
                                if t["type"] == "function" and t["function"]["name"] == "draft_email" and email_count >= max_emails:
                                    continue
                                if t["type"] == "function" and t["function"]["name"] == "manage_memory" and memory_count >= max_memory:
                                    continue
                                if global_pending_edit and t["type"] == "function" and t["function"]["name"] == "propose_edit":
                                    continue
                                available_tools.append(t)

                        payload = {
                            "model": CONFIG["model"],
                            "messages": [
                                {"role": "system", "content": system},
                                *trimmed_history,
                            ],
                            "tools": available_tools if available_tools else [],
                            "options": {
                                "temperature": CONFIG["temperature"],
                                "num_predict": CONFIG["max_tokens"],
                            },
                            "stream": False,
                        }
                        resp = await client.post(f"{CONFIG['ollama_base_url']}/api/chat", json=payload)
                        yield _sse("status", f"ollama responded: {resp.status_code}")
                        resp.raise_for_status()
                        data = resp.json()
                        msg = data.get("message", {})
                        tool_calls = msg.get("tool_calls", [])
                        content = msg.get("content", "")

                    if tool_calls and content:
                        # drop the content, keep tool call
                        content = ""
                    if not tool_calls:
                        conversation_history.append({"role": "assistant", "content": content})
                        for word in content.split(" "):
                            yield _sse("token", word + " ")
                            await asyncio.sleep(0.01)
                        yield _sse("done", json.dumps({
                            "searches_used": search_count,
                            "emails_used": email_count,
                            "tools_used": tool_count,
                            "max_searches": max_searches,
                            "max_emails": max_emails,
                            "max_tools": max_tools
                        }))
                        return

                    if CONFIG["use_groq"]:
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
                    else:
                        tool_calls_for_history = tool_calls

                    conversation_history.append({"role": "assistant", "content": None, "tool_calls": tool_calls_for_history})

                    for tc in tool_calls:
                        if CONFIG["use_groq"]:
                            name = tc.function.name
                            raw_args = tc.function.arguments
                            args = json.loads(raw_args) if raw_args and raw_args != "null" else {}
                        else:
                            fn = tc.get("function", {})
                            name = fn.get("name", "")
                            args = fn.get("arguments", {})

                        yield _sse("tool_requested", json.dumps({"tool": name, "args": args}))

                        if search_count >= max_searches and name in ("web_search", "fetch_webpage"):
                            yield _sse("status", f"Search cap ({max_searches}) reached, answering from context...")
                            tool_call_id = tc.id if CONFIG["use_groq"] else f"call_{name}"
                            conversation_history.append({
                                "role": "tool",
                                "tool_call_id": tool_call_id,
                                "content": f"Search limit of {max_searches} reached."
                            })
                            continue

                        if email_count >= max_emails and name == "draft_email":
                            yield _sse("status", f"Email cap ({max_emails}) reached...")
                            tool_call_id = tc.id if CONFIG["use_groq"] else f"call_{name}"
                            conversation_history.append({
                                "role": "tool",
                                "tool_call_id": tool_call_id,
                                "content": f"Email drafting limit of {max_emails} reached. You cannot draft any more emails in this turn. Please proceed to answer the user based on the drafts already opened."
                            })
                            continue

                        if memory_count >= max_memory and name == "manage_memory":
                            yield _sse("status", f"Memory cap ({max_memory}) reached...")
                            tool_call_id = tc.id if CONFIG["use_groq"] else f"call_{name}"
                            conversation_history.append({
                                "role": "tool",
                                "tool_call_id": tool_call_id,
                                "content": f"Memory management limit of {max_memory} reached. No more memory edits allowed this turn. Please proceed to answer the user."
                            })
                            continue

                        if tool_count >= max_tools:
                            yield _sse("status", f"Total tool cap ({max_tools}) reached...")
                            tool_call_id = tc.id if CONFIG["use_groq"] else f"call_{name}"
                            conversation_history.append({
                                "role": "tool",
                                "tool_call_id": tool_call_id,
                                "content": f"Total tool limit of {max_tools} reached. No more tools can be called. Please proceed to answer the user with the information you already have."
                            })
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

                        result = await mcp.call_tool(name, args)

                        # After fetch_webpage, extract metadata for the frontend card
                        if name == "fetch_webpage":
                            try:
                                # Parse the __LINK_METADATA__ sentinel if your MCP tool emits it
                                lines = result.split("\n")
                                metadata_line = next((l for l in lines if l.startswith("__LINK_METADATA__:")), None)
                                if metadata_line:
                                    metadata = json.loads(metadata_line.split(":", 1)[1])
                                    yield _sse("link_card", json.dumps(metadata))
                                    # Strip the metadata line from what the AI sees
                                    result = "\n".join(l for l in lines if not l.startswith("__LINK_METADATA__:"))
                            except Exception:
                                pass  # non-fatal, just skip the card

                        # Handle action buttons
                        try:
                            lines = result.split("\n")
                            action_line = next((l for l in lines if l.strip().startswith("__ACTION_BUTTON__:")), None)
                            if action_line:
                                log.debug(f"Found action button line: {action_line}")
                                action_data = json.loads(action_line.split(":", 1)[1])
                                yield _sse("action_button", json.dumps(action_data))
                                # Strip the action line from what the AI sees
                                result = "\n".join(l for l in lines if not l.strip().startswith("__ACTION_BUTTON__:"))
                        except Exception as e:
                            log.error(f"Error processing action button: {e}")
                            
                        # Handle propose_edit interception
                        if name == "propose_edit":
                            try:
                                parsed = json.loads(result)
                                if parsed.get("status") == "pending_approval":
                                    tool_call_id = tc.id if CONFIG["use_groq"] else f"call_{name}"
                                    global_pending_edit = {
                                        "toolCallId": tool_call_id,
                                        "path": args.get("path"),
                                        "summary": parsed.get("summary"),
                                        "old_content": parsed.get("old_content"),
                                        "new_content": parsed.get("new_content"),
                                        "target_path": parsed.get("target_path")
                                    }
                                    yield _sse("propose_edit", json.dumps(global_pending_edit))
                                    result = "edit pending approval. provide a short summary of the changes."
                                else:
                                    # Valid JSON but not pending? (Shouldn't happen)
                                    pass
                            except Exception:
                                # It's not JSON, meaning it was REJECTED by validation
                                yield _sse("status", "Edit proposed but failed validation. Agent will retry.")
                                pass # Not JSON or rejected immediately

                        if CONFIG["use_groq"] and groq_client and name in ("web_search", "fetch_webpage"):
                            result = await _summarize_tool_result(groq_client, name, result)
                            yield _sse("status", f"summarized {name} result")

                        if name in ("web_search", "fetch_webpage"):
                            search_count += 1
                        if name == "draft_email":
                            email_count += 1
                        if name == "manage_memory":
                            memory_count += 1
                        
                        tool_count += 1

                        yield _sse("search_result", json.dumps({"tool": name, "count": tool_count}))
                        tool_call_id = tc.id if CONFIG["use_groq"] else f"call_{name}"
                        conversation_history.append({
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "content": result
                        })
                except groq.BadRequestError as e:
                    if hasattr(e, 'response'):
                        log.error("Groq 400 body: %s", e.response.text)
                    if "tool_use_failed" in str(e):
                        log.warning("tool_use_failed — retrying without tools")
                        response = await groq_client.chat.completions.create(
                            model=current_model,
                            messages=[
                                {"role": "system", "content": system},
                                *trimmed_history,
                            ],
                            temperature=CONFIG["temperature"],
                            max_completion_tokens=CONFIG["max_tokens"],
                        )
                        msg = response.choices[0].message
                        content = msg.content or ""

                        conversation_history.append({"role": "assistant", "content": content})
                        for word in content.split(" "):
                            yield _sse("token", word + " ")
                            await asyncio.sleep(0.01)
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
        yield _sse("error", traceback.format_exc())
        yield _sse("done", "{}")