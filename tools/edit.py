import logging
import re
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

def apply_edits(original_content: str, edits: list[dict]) -> str:
    """
    Applies a list of {search, replace} edits to the original content.
    Each edit is a dict with:
      - 'search': the exact text to find (empty string for new-file creation)
      - 'replace': the text to substitute in

    Matching is first attempted case-sensitively. If no match is found,
    a case-insensitive fallback is tried and the matched region is replaced
    while preserving its original characters.
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

        # --- Case-sensitive attempt ---
        if search_text in new_content:
            count = new_content.count(search_text)
            if count > 1:
                raise ValueError(
                    f"Edit #{i+1}: search text matched {count} times. "
                    f"Please provide more context to make it unique.\n{search_text}"
                )
            new_content = new_content.replace(search_text, replace_text)
            continue

        # --- Case-insensitive fallback ---
        log.debug("Edit #%d: exact match not found, trying case-insensitive fallback.", i + 1)
        escaped = re.escape(search_text)
        ci_matches = re.findall(escaped, new_content, flags=re.IGNORECASE)
        if not ci_matches:
            raise ValueError(
                f"Edit #{i+1}: search text not found in file (tried exact and case-insensitive).\n{search_text}"
            )
        if len(ci_matches) > 1:
            raise ValueError(
                f"Edit #{i+1}: case-insensitive search matched {len(ci_matches)} times. "
                f"Please provide more context.\n{search_text}"
            )
        new_content = re.sub(escaped, replace_text.replace("\\", r"\\\\"), new_content, count=1, flags=re.IGNORECASE)

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
