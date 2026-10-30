"""Attachment / file-context rebuild helpers.

All of this is KV-cache-discipline critical: a user message's page images and
extracted text are rebuilt deterministically (byte-identical turn to turn) from
disk so Ollama/llama.cpp keeps the longest-unchanged-prefix KV cache valid.
"""


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
