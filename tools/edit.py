import logging
import re
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Whitespace normalization helpers (used for anchor matching)
# ---------------------------------------------------------------------------

def _build_norm_map(text: str) -> tuple[str, list[int]]:
    """
    Normalize text for whitespace-tolerant, newline-agnostic matching:
      - Treat every whitespace char (space, tab, newline, carriage return)
        as the same class.
      - Collapse any run of whitespace to a single space.
      - Strip leading/trailing whitespace.

    Because newlines collapse to spaces, a single-line ("flat") anchor can
    match a region of the file that spans multiple lines. This is the common
    case: the model copies text it read as one line, but the file wraps that
    text across several physical lines.

    Returns (normalized_text, position_map) where position_map[norm_idx]
    gives the index in the *original* text that produced that normalized char.
    """
    norm: list[str] = []
    pos_map: list[int] = []
    i = 0
    length = len(text)
    line_start = True

    while i < length:
        ch = text[i]

        if ch in (' ', '\t', '\n', '\r'):
            if line_start:
                i += 1  # skip leading whitespace
            else:
                # Collapse any whitespace run (including newlines) to one space
                if not norm or norm[-1] != ' ':
                    norm.append(' ')
                    pos_map.append(i)
                i += 1
        else:
            line_start = False
            norm.append(ch)
            pos_map.append(i)
            i += 1

    # Strip trailing whitespace from the very end
    while norm and norm[-1] == ' ':
        norm.pop()
        pos_map.pop()

    return ''.join(norm), pos_map


# ---------------------------------------------------------------------------
# Anchor finding — 3-tier matching
# ---------------------------------------------------------------------------

def _find_anchor(content: str, anchor: str) -> tuple[int, int]:
    """
    Find anchor text in content. Returns (start, end) indices in original content.
    Tries three tiers:
      1. Case-sensitive exact match
      2. Case-insensitive exact match
      3. Whitespace-tolerant match

    Raises ValueError if not found or ambiguous (multiple matches).
    """
    # --- Tier 1: Case-sensitive ---
    if anchor in content:
        count = content.count(anchor)
        if count > 1:
            raise ValueError(
                f"Anchor matched {count} times (case-sensitive). "
                f"Provide more context to make it unique.\n{anchor}"
            )
        start = content.index(anchor)
        return (start, start + len(anchor))

    # --- Tier 2: Case-insensitive ---
    escaped = re.escape(anchor)
    ci_matches = list(re.finditer(escaped, content, flags=re.IGNORECASE))
    if len(ci_matches) == 1:
        m = ci_matches[0]
        return (m.start(), m.end())
    if len(ci_matches) > 1:
        raise ValueError(
            f"Anchor matched {len(ci_matches)} times (case-insensitive). "
            f"Provide more context.\n{anchor}"
        )

    # --- Tier 3: Whitespace-tolerant ---
    log.debug("Anchor not found with exact/CI, trying whitespace-tolerant.")
    norm_content, content_map = _build_norm_map(content)
    norm_anchor, _ = _build_norm_map(anchor)

    if not norm_anchor:
        raise ValueError(f"Anchor is empty or whitespace-only.\n{anchor}")

    matches = []
    start_pos = 0
    while True:
        idx = norm_content.find(norm_anchor, start_pos)
        if idx == -1:
            break
        matches.append(idx)
        start_pos = idx + 1

    if len(matches) == 0:
        raise ValueError(
            f"Anchor not found (tried exact, case-insensitive, whitespace-tolerant).\n{anchor}"
        )
    if len(matches) > 1:
        raise ValueError(
            f"Anchor matched {len(matches)} times (whitespace-tolerant). "
            f"Provide more context.\n{anchor}"
        )

    match_start_norm = matches[0]
    match_end_norm = matches[0] + len(norm_anchor) - 1

    orig_start = content_map[match_start_norm]
    orig_end = content_map[match_end_norm] + 1

    # Extend to include trailing whitespace (not newlines)
    while orig_end < len(content) and content[orig_end] in (' ', '\t'):
        orig_end += 1

    return (orig_start, orig_end)


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------

def _apply_single_edit(content: str, edit: dict) -> str:
    """
    Apply a single edit to content.

    Edit shape: {action, anchor, content}
      - action: "replace" | "delete" | "insert_before" | "insert_after" | "create"
      - anchor: text to locate in the file (ignored for "create")
      - content: the new text (replacement or insertion content, or full file
        for "create"; ignored for "delete")

    Returns the modified content.
    Raises ValueError on failure.
    """
    action = edit.get("action", "replace")
    anchor = edit.get("anchor", "")
    new_text = edit.get("content", "")

    if action == "create":
        # Full file creation — content is the entire file
        return new_text

    if not anchor:
        raise ValueError(f"Action '{action}' requires a non-empty anchor.")

    # Find the anchor
    start, end = _find_anchor(content, anchor)

    if action == "replace":
        return content[:start] + new_text + content[end:]

    elif action == "delete":
        # Remove exactly the anchored span. If the anchor sat on its own line
        # (line boundary before start, newline right after end), consume one
        # trailing newline so we don't leave a dangling blank line. This is
        # conservative: it only collapses when start begins a line, to avoid
        # merging two unrelated lines together.
        del_end = end
        at_line_start = start == 0 or content[start - 1] == '\n'
        if at_line_start and del_end < len(content) and content[del_end] == '\n':
            del_end += 1  # swallow the now-empty line's newline
        return content[:start] + content[del_end:]

    elif action == "insert_before":
        # Insert content before the anchor. Ensure it ends with a newline
        # so the anchor stays on its own line.
        insertion = new_text if new_text.endswith('\n') else new_text + '\n'
        return content[:start] + insertion + content[start:]

    elif action == "insert_after":
        # Insert content after the anchor. Find the end of the anchor's line.
        # If anchor ends mid-line, extend to the end of that line.
        line_end = end
        while line_end < len(content) and content[line_end] != '\n':
            line_end += 1
        if line_end < len(content):
            line_end += 1  # include the newline

        insertion = new_text if new_text.endswith('\n') else new_text + '\n'
        return content[:line_end] + insertion + content[line_end:]

    else:
        raise ValueError(f"Unknown action: '{action}'. Use replace, delete, insert_before, insert_after, or create.")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def validate_and_preview_edit(workspace_root: Path, edit: dict) -> dict:
    """
    Validate a single edit and compute old/new content preview.

    edit shape: {path, action, anchor, content}

    Returns:
      {
        "path": relative path,
        "old_content": full file before,
        "new_content": full file after this edit,
        "target_path": absolute resolved path
      }

    Raises ValueError on validation failure.
    """
    relative_path = edit.get("path", "")
    if not relative_path:
        raise ValueError(
            "Edit is missing required 'path' field. "
            "Every edit must include 'path' (relative to workspace root, e.g. 'draft.txt' or 'subfolder/file.py')."
        )

    target = (workspace_root / relative_path).resolve()

    try:
        target.relative_to(workspace_root)
    except ValueError:
        raise ValueError("Access denied: path is outside the aiWorkspace folder.")

    if target.is_symlink():
        raise ValueError("Access denied: symlinks not allowed.")

    action = edit.get("action", "replace")
    old_content = ""

    if action == "create":
        # For create, file should not already exist (or we overwrite)
        if target.exists():
            try:
                with target.open("r", encoding="utf-8") as f:
                    old_content = f.read()
            except Exception:
                pass
    else:
        # For other actions, file must exist
        if not target.exists():
            raise ValueError(f"File not found: {relative_path}")
        if not target.is_file():
            raise ValueError("Access denied: target is not a file.")
        try:
            with target.open("r", encoding="utf-8") as f:
                old_content = f.read()
        except Exception as e:
            raise ValueError(f"Failed to read file: {e}")

    new_content = _apply_single_edit(old_content, edit)

    return {
        "path": relative_path,
        "old_content": old_content,
        "new_content": new_content,
        "target_path": str(target),
    }
