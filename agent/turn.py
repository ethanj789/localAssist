import asyncio
import json
import traceback
import logging
from pathlib import Path
from typing import AsyncGenerator

from config import CONFIG
import llm_provider

from .sse import _sse
from .prompt import build_ollama_tools
from .attachments import _apply_file_context, _rehydrate_history_files
from .models import generate_and_save_title, _resolve_turn_models
from .edits import _resolve_preview, _applied_result, _stale_result, _reject_result
from .loop import _run_tool_loop

log = logging.getLogger(__name__)


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
