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


async def _maybe_summarize_history(groq_client, conversation_history: list[dict]) -> None:
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


async def generate_and_save_title(conversation_id: str, first_message: str):
    import db
    try:
        title_prompt = (
            "You are a conversation titler. Generate a short, descriptive 3-5 word title for the conversation that starts with the prompt below. "
            "Do not include quotes, markdown formatting, or any extra text — just return the plain title text.\n\n"
            f"Prompt: {first_message}"
        )
        title = ""
        if CONFIG["use_groq"]:
            from groq import AsyncGroq
            import os
            # Note: We use SUMMARY_MODEL which defaults to llama-3.1-8b-instant
            groq_client = AsyncGroq(api_key=os.getenv("GROQ_API_KEY", ""))
            response = await groq_client.chat.completions.create(
                model=SUMMARY_MODEL,
                messages=[
                    {"role": "user", "content": title_prompt}
                ],
                max_completion_tokens=20,
                temperature=0.5,
            )
            title = response.choices[0].message.content.strip()
        else:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                payload = {
                    "model": CONFIG["model"],
                    "messages": [
                        {"role": "user", "content": title_prompt}
                    ],
                    "options": {
                        "temperature": 0.5,
                        "num_predict": 20,
                    },
                    "stream": False,
                }
                resp = await client.post(f"{CONFIG['ollama_base_url']}/api/chat", json=payload)
                if resp.status_code == 200:
                    title = resp.json().get("message", {}).get("content", "").strip()
        
        title = title.strip().strip('"').strip("'").strip()
        if title:
            title = title[:50]  # truncate to prevent layout issues
            db.update_conversation_title(conversation_id, title)
            log.info(f"Generated title for {conversation_id}: {title}")
    except Exception as e:
        log.error(f"Failed to generate title for {conversation_id}: {e}")


async def agent_loop(user_message: str, mcp, model_override: str = "default", conversation_id: str = None) -> AsyncGenerator[str, None]:
    global global_pending_edit
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
            "groq" if CONFIG["use_groq"] else "ollama"
        )
        use_groq = resolved_provider == "groq"

        if use_groq:
            if model_override == "coding":
                tool_model, answer_model = CONFIG["groq_model"], CODING_MODEL
            elif model_override == "thinking":
                tool_model, answer_model = CONFIG["groq_model"], THINKING_MODEL
            else:
                tool_model, answer_model = select_groq_model(user_message)

            if answer_model != CONFIG["groq_model"]:
                yield _sse("model_upgrade", answer_model)
        else:
            tool_model = CONFIG["model"]
            answer_model = CONFIG["model"]

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
        yield _sse("status", f"calling {resolved_provider}")

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
                    if use_groq:
                        from groq import AsyncGroq
                        groq_client = AsyncGroq(api_key=os.getenv("GROQ_API_KEY", ""))
                        await _maybe_summarize_history(groq_client, conversation_history)

                    memory = load_memory()
                    system = CONFIG["system_prompt"] + "\n\n" + CONFIG["memory_management_prompt"]
                    if memory:
                        system = system + "\n\n" + memory
                        
                    if global_edit_log:
                        system += "\n\nEdit Log:\n" + "\n".join(global_edit_log)

                    if use_groq:
                        try: 
                            yield _sse("model", json.dumps({
                                "provider": "groq",
                                "model": current_model
                            }))

                            available_tools = []
                            last_was_pending_edit = len(trimmed_history) > 0 and trimmed_history[-1].get("role") == "tool" and trimmed_history[-1].get("content") == "edit pending approval"
                            
                            if last_was_pending_edit:
                                available_tools = []
                            else:
                                for t in ollama_tools:
                                    if tool_count >= max_tools:
                                        break
                                    if t["function"]["name"] in ("web_search", "fetch_webpage") and search_count >= max_searches:
                                        continue
                                    if t["function"]["name"] == "draft_email" and email_count >= max_emails:
                                        continue
                                    if t["function"]["name"] == "save_user_preference" and memory_count >= max_memory:
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
                                if t["type"] == "function" and t["function"]["name"] == "save_user_preference" and memory_count >= max_memory:
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
                        content = ""
                    if not tool_calls:
                        conversation_history.append({"role": "assistant", "content": content})
                        db.add_message(conversation_id, "assistant", content)
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

                    if use_groq:
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
                        # tool_calls_for_history = tool_calls
                        tool_calls_for_history = [
                            {
                                "id": f"call_{tc.get('function',{}).get('name','tool')}_{i}",
                                "type": "function",
                                "function": tc.get("function", {})
                            }
                            for i, tc in enumerate(tool_calls)
                        ]  

                    conversation_history.append({"role": "assistant", "content": None, "tool_calls": tool_calls_for_history})
                    db.add_message(conversation_id, "assistant", None, tool_calls=tool_calls_for_history)

                    for tc in tool_calls:
                        if use_groq:
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
                            tool_call_id = tc.id if use_groq else f"call_{name}"
                            conversation_history.append({
                                "role": "tool",
                                "tool_call_id": tool_call_id,
                                "content": f"Search limit of {max_searches} reached."
                            })
                            db.add_message(conversation_id, "tool", f"Search limit of {max_searches} reached.", tool_call_id=tool_call_id)
                            continue

                        if email_count >= max_emails and name == "draft_email":
                            yield _sse("status", f"Email cap ({max_emails}) reached...")
                            tool_call_id = tc.id if use_groq else f"call_{name}"
                            conversation_history.append({
                                "role": "tool",
                                "tool_call_id": tool_call_id,
                                "content": f"Email drafting limit of {max_emails} reached. You cannot draft any more emails in this turn. Please proceed to answer the user based on the drafts already opened."
                            })
                            db.add_message(conversation_id, "tool", f"Email drafting limit of {max_emails} reached.", tool_call_id=tool_call_id)
                            continue

                        if memory_count >= max_memory and name == "save_user_preference":
                            yield _sse("status", f"Memory cap ({max_memory}) reached...")
                            tool_call_id = tc.id if use_groq else f"call_{name}"
                            conversation_history.append({
                                "role": "tool",
                                "tool_call_id": tool_call_id,
                                "content": f"Memory management limit of {max_memory} reached. No more memory edits allowed this turn. Please proceed to answer the user."
                            })
                            db.add_message(conversation_id, "tool", f"Memory management limit of {max_memory} reached.", tool_call_id=tool_call_id)
                            continue

                        if tool_count >= max_tools:
                            yield _sse("status", f"Total tool cap ({max_tools}) reached...")
                            tool_call_id = tc.id if use_groq else f"call_{name}"
                            conversation_history.append({
                                "role": "tool",
                                "tool_call_id": tool_call_id,
                                "content": f"Total tool limit of {max_tools} reached. No more tools can be called. Please proceed to answer the user with the information you already have."
                            })
                            db.add_message(conversation_id, "tool", f"Total tool limit of {max_tools} reached.", tool_call_id=tool_call_id)
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
                                    tool_call_id = tc.id if use_groq else f"call_{name}"
                                    global_pending_edit = {
                                        "toolCallId": tool_call_id,
                                        "path": args.get("path"),
                                        "summary": parsed.get("summary"),
                                        "old_content": parsed.get("old_content"),
                                        "new_content": parsed.get("new_content"),
                                        "target_path": parsed.get("target_path"),
                                        "conversation_id": conversation_id
                                    }
                                    yield _sse("propose_edit", json.dumps(global_pending_edit))
                                    result = "edit pending approval. provide a short summary of the changes."
                            except Exception:
                                yield _sse("status", "Edit proposed but failed validation. Agent will retry.")
                                pass

                        if use_groq and groq_client and name in ("web_search", "fetch_webpage"):
                            result = await _summarize_tool_result(groq_client, name, result)
                            yield _sse("status", f"summarized {name} result")

                        if name in ("web_search", "fetch_webpage"):
                            search_count += 1
                        if name == "draft_email":
                            email_count += 1
                        if name == "save_user_preference":
                            memory_count += 1
                        
                        tool_count += 1

                        yield _sse("search_result", json.dumps({"tool": name, "count": tool_count}))
                        tool_call_id = tc.id if use_groq else f"call_{name}"
                        conversation_history.append({
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "content": result
                        })
                        db.add_message(conversation_id, "tool", result, tool_call_id=tool_call_id)
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
                        db.add_message(conversation_id, "assistant", content)
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
