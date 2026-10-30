"""Edit resolution + the frozen contract result strings.

This is the agent-side edit module: it resolves a proposed edit against the
current file (`_resolve_preview`) and owns the frozen tool-result strings the
agent turn feeds back to the model for each edit outcome
(applied/stale/invalid/read-only/reject) plus the extra-edit auto-reject
message. These are agent-turn contract vocabulary, not edit mechanics (those
live in tools/edit.py) and not configuration (so they don't belong in config).

`REQUIRES_CONFIRMATION` is the one exception: `db.get_pending_tool_call` also
needs it, and `db` must not import `agent` (that would form a db<->agent import
cycle). So the confirm-required set lives in `config` (a leaf module) and is
re-exported here for the agent package's own use.
"""

from config import REQUIRES_CONFIRMATION

# Emitted when more than one confirm-required edit is proposed in a single tool
# batch — only one edit can be pending at a time, so extras are auto-rejected.
AUTO_REJECT_EXTRA_EDIT = (
    "Only one edit can be pending approval at a time. This additional edit was "
    "NOT queued or saved. After the current pending edit is approved or "
    "rejected, propose this edit again. (Note: edits are resolved one at a time "
    "as the user approves them, not once per user message.)"
)


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


def _read_only_edit_result(msg: str) -> str:
    """Tool result for an edit targeting a read-only path (policy denial). This
    is terminal — the model must NOT retry or re-propose, since no anchor or
    content change can make the path writable."""
    return (
        f"Edit NOT applied — {msg} Do NOT retry this edit or propose another "
        "edit to the same path. If the user wants this content changed, tell "
        "them it must be edited in the Notes app, not here."
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


__all__ = [
    "REQUIRES_CONFIRMATION",
    "AUTO_REJECT_EXTRA_EDIT",
    "_applied_result",
    "_stale_result",
    "_invalid_edit_result",
    "_read_only_edit_result",
    "_reject_result",
    "_resolve_preview",
]


def _resolve_preview(edit_inputs: dict) -> dict:
    """Resolve an edit's inputs against the file AS IT IS NOW and return the
    contract ``preview`` shape. Never raises — a stale/invalid edit becomes
    ``{"ok": false, "error": "<msg>"}``. Used by the propose path, the resume
    path, and GET /pending_edit so all three render/apply against current state.

    On success: ``{"ok": true, "old_content", "new_content", "target_path"}``."""
    from tools.files import WORKSPACE_ROOT
    from tools.edit import resolve_edit, ReadOnlyPathError

    edit = {
        "path": edit_inputs.get("path", ""),
        "action": edit_inputs.get("action", "replace"),
        "anchor": edit_inputs.get("anchor", ""),
        "content": edit_inputs.get("content", ""),
    }
    try:
        resolved = resolve_edit(WORKSPACE_ROOT, edit)
    except ReadOnlyPathError as e:
        # Permanent policy denial (e.g. notesAppText/). Flag it so the gate can
        # reject with a clear reason and NOT burn anchor-retry attempts.
        return {"ok": False, "error": str(e), "read_only": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {
        "ok": True,
        "old_content": resolved["old_content"],
        "new_content": resolved["new_content"],
        "target_path": resolved["target_path"],
    }
