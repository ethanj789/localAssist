import difflib
import logging
import re
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


class ReadOnlyPathError(ValueError):
    """Raised when an edit targets a path the agent may never write (a policy
    denial, not a fixable validation error). Subclasses ValueError so existing
    ``except ValueError`` handlers still catch it, while callers that care can
    detect it explicitly and skip the pointless anchor-retry loop."""


# ---------------------------------------------------------------------------
# Invalid-JSON-escape sanitizer (applied to parsed tool-call arguments)
# ---------------------------------------------------------------------------

# The set of characters that legally follow a backslash in a JSON string.
# (JSON spec: \" \\ \/ \b \f \n \r \t \uXXXX)
_JSON_LEGAL_ESCAPE_CHARS = frozenset('"\\/bfnrtu')


def _strip_invalid_json_escapes_str(s: str) -> str:
    """
    Remove backslashes that precede a character JSON does not allow as an
    escape. Small local models sometimes emit non-standard escapes (e.g. ``\\``'
    before a backtick or ``$``) inside tool-call argument strings; lenient JSON
    parsing leaves a stray literal backslash in the value, which then corrupts
    written file content and anchor matching.

    A backslash is dropped ONLY when the next character is outside the
    JSON-legal escape set (" \\ / b f n r t u). Legal escapes have already been
    decoded by the JSON parser into real characters (newline, tab, quote, etc.),
    so any backslash still present here is either a legitimate literal backslash
    followed by a legal-escape char (left alone, conservative) or a stray
    backslash from a bogus escape (removed).

    Examples (on the already-parsed Python string):
        r"cell-\\${i}"   -> "cell-${i}"
        r"say \\`hi\\`"  -> "say `hi`"
        "line\\nbreak"   -> "line\\nbreak"  (backslash before 'n' preserved)

    Idempotent: a string with no stray backslashes is returned unchanged, and
    running the function twice yields the same result.
    """
    if '\\' not in s:
        return s

    out = []
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == '\\' and i + 1 < n and s[i + 1] not in _JSON_LEGAL_ESCAPE_CHARS:
            # Stray backslash from an invalid JSON escape — drop it, keep the
            # following character verbatim.
            out.append(s[i + 1])
            i += 2
        else:
            out.append(ch)
            i += 1
    return ''.join(out)


def strip_invalid_json_escapes(value):
    """
    Recursively sanitize invalid JSON escape sequences out of a parsed
    tool-call argument value.

    Walks dicts and lists; cleans str leaves via _strip_invalid_json_escapes_str.
    Non-string scalars (int, float, bool, None) pass through untouched. Pure and
    idempotent — it only ever removes stray backslashes, never adds characters,
    so a legitimate bare ``$`` or backtick is left exactly as-is.
    """
    if isinstance(value, str):
        return _strip_invalid_json_escapes_str(value)
    if isinstance(value, dict):
        return {k: strip_invalid_json_escapes(v) for k, v in value.items()}
    if isinstance(value, list):
        return [strip_invalid_json_escapes(v) for v in value]
    return value


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


# Decoration characters that small models routinely add, drop, or mangle when
# copying an anchor out of markdown-ish text: emphasis/heading/quote markers and
# the smart-quote variants of ' and ". Tier 4 neutralizes these so an anchor
# like "Summary:" still matches "**Summary:**" in the file. Kept deliberately
# small — we only strip presentational noise, never letters/digits/structure.
_MD_DECORATION_CHARS = frozenset('*_`#>~')
_SMART_QUOTE_MAP = {
    '\u2018': "'", '\u2019': "'",   # ' '
    '\u201c': '"', '\u201d': '"',   # " "
    '\u2013': '-', '\u2014': '-',   # en/em dash
}


def _build_md_norm_map(text: str) -> tuple[str, list[int]]:
    """
    Like ``_build_norm_map`` (whitespace-collapsing, newline-agnostic) but ALSO
    drops markdown decoration characters (``* _ ` # > ~``) and folds smart
    quotes/dashes to their ASCII form. Used only as the last-resort matching
    tier, so a model that writes ``Summary:`` or ``"quote"`` still lands on the
    file's ``**Summary:**`` / ``"quote"``.

    Returns (normalized_text, position_map) where position_map[i] is the index
    in the ORIGINAL text that produced normalized char i, so a match can be
    mapped back to an exact original span.
    """
    norm: list[str] = []
    pos_map: list[int] = []
    i = 0
    length = len(text)
    line_start = True

    while i < length:
        ch = text[i]
        folded = _SMART_QUOTE_MAP.get(ch, ch)

        if folded in (' ', '\t', '\n', '\r'):
            if line_start:
                i += 1
            else:
                if not norm or norm[-1] != ' ':
                    norm.append(' ')
                    pos_map.append(i)
                i += 1
        elif folded in _MD_DECORATION_CHARS:
            # Drop the decoration char entirely; do NOT flip line_start, so a
            # line that opens with "## " or "> " still has its leading space run
            # treated as leading (skipped) rather than becoming a real space.
            i += 1
        else:
            line_start = False
            norm.append(folded)
            pos_map.append(i)
            i += 1

    while norm and norm[-1] == ' ':
        norm.pop()
        pos_map.pop()

    return ''.join(norm), pos_map


# ---------------------------------------------------------------------------
# Rich failure context — show the model the ACTUAL file text near its miss
# ---------------------------------------------------------------------------

def _closest_snippets(content: str, anchor: str, limit: int = 2) -> list[str]:
    """
    Find the passages of ``content`` that most resemble ``anchor`` so a failed
    match can tell the model what to copy instead of just echoing its own bad
    anchor back. Slides a window the size of the anchor across the file (line by
    line, newline-agnostic) and ranks by difflib similarity. Returns up to
    ``limit`` distinct original-text snippets, best first. Best-effort: returns
    [] rather than raising on any edge case.
    """
    try:
        norm_anchor, _ = _build_norm_map(anchor)
        if not norm_anchor:
            return []
        lines = content.splitlines(keepends=True)
        if not lines:
            return []

        # Choose a window (in lines) roughly covering the anchor's length.
        approx_lines = max(1, anchor.count('\n') + 1)
        window = min(len(lines), approx_lines + 2)

        matcher = difflib.SequenceMatcher(a=norm_anchor)
        scored: list[tuple[float, str]] = []
        for i in range(len(lines)):
            chunk = ''.join(lines[i:i + window])
            norm_chunk, _ = _build_norm_map(chunk)
            if not norm_chunk:
                continue
            matcher.set_seq2(norm_chunk)
            ratio = matcher.quick_ratio()
            if ratio > 0.4:
                scored.append((ratio, chunk.strip('\n')))

        scored.sort(key=lambda t: t[0], reverse=True)
        out: list[str] = []
        seen: set[str] = set()
        for _ratio, snip in scored:
            key = snip.strip()
            if key and key not in seen:
                seen.add(key)
                out.append(snip)
            if len(out) >= limit:
                break
        return out
    except Exception:
        return []


def _format_snippets(snippets: list[str]) -> str:
    """Render snippet list for an error message, each clearly delimited so the
    model can copy one verbatim as its next anchor."""
    if not snippets:
        return ""
    parts = ["\nClosest matching text in the file (copy one of these EXACTLY as your anchor):"]
    for s in snippets:
        parts.append("-----")
        parts.append(s)
        parts.append("-----")
    return "\n".join(parts)


def _match_contexts(content: str, spans: list[tuple[int, int]], limit: int = 4) -> str:
    """For an ambiguous ('too many matches') anchor, show each occurrence with a
    little surrounding text so the model can see what DISTINGUISHES them and pick
    a longer unique anchor. Spans are (start, end) indices into ``content``."""
    parts = [
        "\nThe anchor occurs at these locations (shown with the line before and "
        "after each — add some of that neighboring text to your anchor to target ONE):"
    ]
    for idx, (start, end) in enumerate(spans[:limit], 1):
        # Show the matched line plus ONE neighboring line on each side. For
        # identical repeated lines the match itself is not distinguishing, so the
        # neighbor lines are exactly what the model must fold into its anchor.
        line_start = content.rfind("\n", 0, start) + 1
        prev_start = content.rfind("\n", 0, line_start - 1) + 1 if line_start > 0 else 0
        line_end = content.find("\n", end)
        line_end = len(content) if line_end == -1 else line_end
        next_end = content.find("\n", line_end + 1)
        next_end = len(content) if next_end == -1 else next_end
        snippet = content[prev_start:next_end].replace("\n", "⏎")
        parts.append(f"  {idx}. {snippet}")
    if len(spans) > limit:
        parts.append(f"  … and {len(spans) - limit} more.")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Anchor finding — 4-tier matching
# ---------------------------------------------------------------------------

def _norm_find_all(content: str, anchor: str, md: bool) -> list[tuple[int, int]]:
    """Run a single normalized matching pass and return ALL original-text spans
    it resolves to. ``md=False`` is the whitespace-tolerant tier; ``md=True``
    additionally neutralizes markdown decoration + smart quotes. Returns [] when
    the (normalized) anchor is empty or not present."""
    build = _build_md_norm_map if md else _build_norm_map
    norm_content, content_map = build(content)
    norm_anchor, _ = build(anchor)
    if not norm_anchor:
        return []

    spans: list[tuple[int, int]] = []
    start_pos = 0
    while True:
        idx = norm_content.find(norm_anchor, start_pos)
        if idx == -1:
            break
        match_end_norm = idx + len(norm_anchor) - 1
        orig_start = content_map[idx]
        orig_end = content_map[match_end_norm] + 1
        # Extend to include trailing spaces/tabs (not newlines).
        while orig_end < len(content) and content[orig_end] in (' ', '\t'):
            orig_end += 1
        spans.append((orig_start, orig_end))
        start_pos = idx + 1
    return spans


def _find_anchor(content: str, anchor: str, find_all: bool = False):
    """
    Find anchor text in content. Returns (start, end) indices in original
    content — or, when ``find_all`` is True, a list of ALL non-overlapping
    (start, end) spans (for replace_all).

    Four tiers, tried in order and stopping at the first that finds ANY match:
      1. Case-sensitive exact
      2. Case-insensitive
      3. Whitespace-tolerant (newline-agnostic)
      4. Markdown/punctuation-insensitive (drops * _ ` # > ~, folds smart quotes)

    Raises ValueError if not found, or — when ``find_all`` is False — if the
    anchor is ambiguous (matches more than once). Failure messages carry the
    ACTUAL nearby file text so the model can fix the anchor instead of guessing.
    """
    def _result(spans: list[tuple[int, int]]):
        return list(spans) if find_all else spans[0]

    # --- Tier 1: Case-sensitive exact ---
    if anchor in content:
        spans = []
        start_pos = 0
        while True:
            idx = content.find(anchor, start_pos)
            if idx == -1:
                break
            spans.append((idx, idx + len(anchor)))
            start_pos = idx + len(anchor)
        if find_all:
            return spans
        if len(spans) > 1:
            raise ValueError(
                f"Anchor matched {len(spans)} times (case-sensitive). "
                "Add surrounding words to target exactly one occurrence, or set "
                "replace_all=true to change every occurrence."
                + _match_contexts(content, spans)
            )
        return spans[0]

    # --- Tier 2: Case-insensitive ---
    escaped = re.escape(anchor)
    ci = [(m.start(), m.end()) for m in re.finditer(escaped, content, flags=re.IGNORECASE)]
    if ci:
        if find_all:
            return ci
        if len(ci) > 1:
            raise ValueError(
                f"Anchor matched {len(ci)} times (case-insensitive). "
                "Add surrounding words to target exactly one occurrence, or set "
                "replace_all=true to change every occurrence."
                + _match_contexts(content, ci)
            )
        return ci[0]

    # --- Tier 3: Whitespace-tolerant ---
    log.debug("Anchor not found with exact/CI, trying whitespace-tolerant.")
    if not _build_norm_map(anchor)[0]:
        raise ValueError(f"Anchor is empty or whitespace-only.\n{anchor}")
    ws = _norm_find_all(content, anchor, md=False)
    if ws:
        if find_all:
            return ws
        if len(ws) > 1:
            raise ValueError(
                f"Anchor matched {len(ws)} times (whitespace-tolerant). "
                "Add surrounding words to target exactly one occurrence, or set "
                "replace_all=true to change every occurrence."
                + _match_contexts(content, ws)
            )
        return ws[0]

    # --- Tier 4: Markdown / punctuation-insensitive (last resort) ---
    log.debug("Anchor not found with whitespace-tolerant, trying markdown-insensitive.")
    mdm = _norm_find_all(content, anchor, md=True)
    if mdm:
        if find_all:
            return mdm
        if len(mdm) > 1:
            raise ValueError(
                f"Anchor matched {len(mdm)} times (ignoring markdown formatting). "
                "Add surrounding words to target exactly one occurrence, or set "
                "replace_all=true to change every occurrence."
                + _match_contexts(content, mdm)
            )
        return mdm[0]

    # --- Nothing matched in any tier: show the closest actual text ---
    raise ValueError(
        "Anchor not found (tried exact, case-insensitive, whitespace-tolerant, "
        "and markdown-insensitive). The text you gave does not appear in the file."
        + _format_snippets(_closest_snippets(content, anchor))
    )


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
    replace_all = bool(edit.get("replace_all", False))

    if action == "create":
        # Full file creation — content is the entire file
        return new_text

    if not anchor:
        raise ValueError(f"Action '{action}' requires a non-empty anchor.")

    # replace_all only makes sense for span-affecting actions. For insert it is
    # ambiguous (insert before/after WHICH occurrence?), so reject it loudly.
    if replace_all and action not in ("replace", "delete"):
        raise ValueError(
            f"replace_all is only valid with action 'replace' or 'delete', not '{action}'."
        )

    if replace_all:
        # Resolve EVERY occurrence and rewrite right-to-left so earlier indices
        # stay valid as we splice. One anchor, many sites — blast radius is the
        # match count (surfaced to the user in the summary/preview).
        spans = _find_anchor(content, anchor, find_all=True)
        for start, end in sorted(spans, key=lambda s: s[0], reverse=True):
            if action == "replace":
                content = content[:start] + new_text + content[end:]
            else:  # delete
                del_end = end
                at_line_start = start == 0 or content[start - 1] == '\n'
                if at_line_start and del_end < len(content) and content[del_end] == '\n':
                    del_end += 1
                content = content[:start] + content[del_end:]
        return content

    # Find the single anchor (raises on ambiguity).
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

def resolve_edit(workspace_root: Path, edit: dict) -> dict:
    """
    Validate a single edit and compute old/new content preview.

    Renamed from validate_and_preview_edit — "preview" vs "apply" is purely
    what the caller does with the result (render the diff vs write new_content
    to target_path). This function just resolves the edit against the file as
    it is NOW.

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
        rel = target.relative_to(workspace_root)
    except ValueError:
        raise ValueError("Access denied: path is outside the aiWorkspace folder.")

    # notesAppText/ holds handwritten-note text owned by the Notes app. It is
    # searchable and readable, but read-only to the agent: the text is
    # regenerated from pen strokes on every save, so any edit here would be
    # silently overwritten. Block all writes under this subtree. This is a
    # permanent policy denial (ReadOnlyPathError), not a retryable failure.
    if rel.parts and rel.parts[0] == "notesAppText":
        raise ReadOnlyPathError(
            "Access denied: notesAppText/ is managed by the Notes app and is read-only. "
            "You can read and search note text, but it cannot be edited here — "
            "its content comes from the user's handwritten strokes."
        )

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
