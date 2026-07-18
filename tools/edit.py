import logging
import re
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Whitespace normalization helpers
# ---------------------------------------------------------------------------

def _build_norm_map(text: str) -> tuple[str, list[int]]:
    """
    Normalize text for whitespace-tolerant matching:
      - Strip leading/trailing whitespace from each line.
      - Collapse internal whitespace runs (spaces/tabs) to a single space.
      - Preserve newlines as explicit boundary tokens.

    Returns (normalized_text, position_map) where position_map[norm_idx]
    gives the index in the *original* text that produced that normalized char.
    """
    norm: list[str] = []
    pos_map: list[int] = []

    for line in text.split('\n'):
        # If not the first line, append the newline separator
        if norm and norm[-1] != '\n':
            # Find the newline position in the original text.
            # We track it by looking at the last mapped original position and
            # scanning forward to the newline.
            pass  # handled below

        stripped = line.strip()
        if not stripped:
            # Empty/whitespace-only line → keep as a single newline boundary
            continue  # will be represented by the '\n' appended between lines

        # Collapse internal whitespace runs
        prev_was_space = False
        for ch in stripped:
            if ch in (' ', '\t'):
                if not prev_was_space:
                    norm.append(' ')
                    # pos_map entry for collapsed space — use first char position
                    # (not critical for replacement, just needs to be in range)
                    pos_map.append(-1)  # placeholder, filled in pass 2
                prev_was_space = True
            else:
                norm.append(ch)
                pos_map.append(-1)  # placeholder
                prev_was_space = False

    # The simple approach above loses positional info. Let's do a proper
    # single-pass that keeps the original index tracking.
    # -- Re-implement with correct tracking --
    norm = []
    pos_map = []
    i = 0
    length = len(text)
    line_start = True  # are we in leading whitespace?

    while i < length:
        ch = text[i]

        if ch == '\n':
            # Before appending the newline, strip any trailing space we may
            # have just added (collapse trailing ws on the line).
            while norm and norm[-1] == ' ':
                norm.pop()
                pos_map.pop()
            norm.append('\n')
            pos_map.append(i)
            line_start = True
            i += 1
        elif ch in (' ', '\t'):
            if line_start:
                # Skip leading whitespace
                i += 1
            else:
                # Internal whitespace: collapse to single space
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


def _whitespace_tolerant_find(content: str, search_text: str) -> tuple[int, int] | None:
    """
    Attempt to find search_text in content using whitespace-normalized matching.
    Returns (start, end) character indices in the *original* content, or None.
    Raises ValueError if more than one match is found.
    """
    norm_content, content_map = _build_norm_map(content)
    norm_search, _ = _build_norm_map(search_text)

    if not norm_search:
        return None

    # Find all occurrences in normalized space
    matches = []
    start_pos = 0
    while True:
        idx = norm_content.find(norm_search, start_pos)
        if idx == -1:
            break
        matches.append(idx)
        start_pos = idx + 1

    if len(matches) == 0:
        return None
    if len(matches) > 1:
        raise ValueError(f"whitespace-tolerant search matched {len(matches)} times")

    match_start_norm = matches[0]
    match_end_norm = matches[0] + len(norm_search) - 1

    # Map back to original positions
    orig_start = content_map[match_start_norm]
    orig_end = content_map[match_end_norm]

    # Extend orig_end to include the full character (it points to the last
    # matched char's start position in original). We want the slice to go
    # one past it.
    orig_end += 1

    # Extend to include any trailing whitespace up to (but not including) the
    # next newline or non-space char, so the replacement is clean.
    while orig_end < len(content) and content[orig_end] in (' ', '\t'):
        orig_end += 1

    return (orig_start, orig_end)


# ---------------------------------------------------------------------------
# Main edit logic
# ---------------------------------------------------------------------------

def apply_edits(original_content: str, edits: list[dict]) -> str:
    """
    Applies a list of {search, replace} edits to the original content.
    Each edit is a dict with:
      - 'search': the exact text to find (empty string for new-file creation)
      - 'replace': the text to substitute in

    Matching tiers:
      1. Case-sensitive exact match
      2. Case-insensitive exact match
      3. Whitespace-tolerant match (normalized whitespace, preserving newline boundaries)
    """
    if not edits:
        return original_content

    new_content = original_content
    for i, edit in enumerate(edits):
        search_text: str = edit.get("search", "")
        replace_text: str = edit.get("replace", "")

        # New-file shortcut: empty search means replace everything
        if search_text == "":
            new_content = replace_text
            continue

        # --- Tier 1: Case-sensitive attempt ---
        if search_text in new_content:
            count = new_content.count(search_text)
            if count > 1:
                raise ValueError(
                    f"Edit #{i+1}: search text matched {count} times. "
                    f"Please provide more context to make it unique.\n{search_text}"
                )
            new_content = new_content.replace(search_text, replace_text)
            continue

        # --- Tier 2: Case-insensitive fallback ---
        log.debug("Edit #%d: exact match not found, trying case-insensitive fallback.", i + 1)
        escaped = re.escape(search_text)
        ci_matches = re.findall(escaped, new_content, flags=re.IGNORECASE)
        if len(ci_matches) == 1:
            new_content = re.sub(escaped, replace_text.replace("\\", r"\\\\"), new_content, count=1, flags=re.IGNORECASE)
            continue
        if len(ci_matches) > 1:
            raise ValueError(
                f"Edit #{i+1}: case-insensitive search matched {len(ci_matches)} times. "
                f"Please provide more context.\n{search_text}"
            )

        # --- Tier 3: Whitespace-tolerant fallback ---
        log.debug("Edit #%d: case-insensitive not found, trying whitespace-tolerant match.", i + 1)
        try:
            span = _whitespace_tolerant_find(new_content, search_text)
        except ValueError as e:
            raise ValueError(
                f"Edit #{i+1}: {e}. Please provide more context.\n{search_text}"
            )

        if span is None:
            raise ValueError(
                f"Edit #{i+1}: search text not found in file "
                f"(tried exact, case-insensitive, and whitespace-tolerant).\n{search_text}"
            )

        start, end = span
        new_content = new_content[:start] + replace_text + new_content[end:]

    return new_content

def validate_and_apply_edit(workspace_root: Path, relative_path: str, edits: list[dict]) -> dict:
    """
    Validates the path and applies a list of {search, replace} edits.
    Returns the old and new content.
    Raises ValueError if validation fails.
    """
    target = (workspace_root / relative_path).resolve()
    
    try:
        target.relative_to(workspace_root)
    except ValueError:
        raise ValueError("Access denied: path is outside the aiWorkspace folder.")
        
    if target.is_symlink():
        raise ValueError("Access denied: symlinks not allowed.")
        
    old_content = ""
    if target.exists():
        if not target.is_file():
            raise ValueError("Access denied: target is not a file.")
        
        try:
            with target.open("r", encoding="utf-8") as f:
                old_content = f.read()
        except Exception as e:
            raise ValueError(f"Failed to read file: {e}")
            
    new_content = apply_edits(old_content, edits)
        
    return {
        "old_content": old_content,
        "new_content": new_content,
        "target_path": str(target)
    }
