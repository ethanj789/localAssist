"""
tools/files.py — Workspace file tool implementations.
Provides list_files, read_file, and read_code_skeleton with strict
path-traversal and symlink guards.
"""
import os
import re
import ast
import json
import fnmatch
import logging
from difflib import SequenceMatcher
from pathlib import Path
from mcp import types

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

# Path(__file__).parent is tools/, so .parent.parent is the project root
WORKSPACE_ROOT = (Path(__file__).parent.parent / os.getenv("WORKSPACE", "aiWorkspace")).resolve()

MAX_FILE_LINES = 250
MAX_SCAN_BYTES = 1_000_000

SKIP_DIRS = {
    "node_modules", ".git", "venv", ".venv", "__pycache__",
    "dist", "build", ".mypy_cache", ".pytest_cache", ".index", ".stfolder", "aiNotes",
    "notesAppText",  # OCR output — indexed via note_blobs table, not file_chunks
}

SCANNABLE_EXTENSIONS = {
    ".py", ".txt", ".md", ".json", ".yaml", ".yml", ".toml",
    ".ts", ".tsx", ".js", ".jsx", ".html", ".css", ".sh",
    ".env.example", ".java",
}

_RAW_SENSITIVE_FILENAMES = {
    ".env", ".env.local", ".env.production", ".env.development",
    "id_rsa", "id_ed25519", ".netrc", ".htpasswd",
}
_RAW_SENSITIVE_PATTERNS = {
    "key", "secret", "token", "password", "credential",
}

SENSITIVE_FILENAMES = {s.lower() for s in _RAW_SENSITIVE_FILENAMES}
SENSITIVE_PATTERNS  = {s.lower() for s in _RAW_SENSITIVE_PATTERNS}


# ── Security helpers ──────────────────────────────────────────────────────────

def _is_sensitive(filename: str, full_path: str | None = None) -> bool:
    name = filename.lower()
    if name in SENSITIVE_FILENAMES or any(p in name for p in SENSITIVE_PATTERNS):
        return True
    if full_path:
        norm = full_path.replace("\\", "/").lower()
        parts = norm.split("/")
        if any(part in SENSITIVE_FILENAMES for part in parts):
            return True
        if any(p in norm for p in SENSITIVE_PATTERNS):
            return True
    return False


def _is_scannable(path: Path, rel_path: str = "") -> bool:
    if path.is_symlink():
        return False
    name = path.name.lower()
    if name != ".env.example" and path.suffix.lower() not in SCANNABLE_EXTENSIONS:
        return False
    if _is_sensitive(path.name, rel_path):
        return False
    return True


def _has_symlink_component(path: Path) -> bool:
    for parent in reversed(path.parents):
        if parent.is_symlink():
            return True
    return path.is_symlink()


def _find_topic_ranges(
    file_lines: list[str], topic: str, context: int = 2
) -> list[tuple[int, int]]:
    """Return merged 1-based [start, end] line ranges where topic appears."""
    n = len(file_lines)
    hit_indices = [i for i, line in enumerate(file_lines) if topic in line.lower()]
    if not hit_indices:
        return []

    ranges: list[tuple[int, int]] = []
    for i in hit_indices:
        lo = max(0, i - context)
        hi = min(n - 1, i + context)
        if ranges and lo <= ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], hi))
        else:
            ranges.append((lo, hi))

    return [(s + 1, e + 1) for s, e in ranges]


# ── Note-path resolution ──────────────────────────────────────────────────────

# Matches patterns like "note: <page_id> / blob <N>" (with optional brackets)
_NOTE_PATH_RE = re.compile(
    r"^\[?note:\s*(?P<page_id>[^\]/]+?)\s*/\s*blob\s+(?P<blob_idx>\d+)\]?$",
    re.IGNORECASE,
)

NOTES_TEXT_DIR = WORKSPACE_ROOT.parent / "aiWorkspace" / "notesAppText"


def _resolve_note_path(path: str) -> Path | None:
    """
    If *path* looks like a note reference (e.g. "note: 2026-07-13_untitled / blob 0"),
    resolve it to the actual .txt file under notesAppText by scanning .page_meta.json
    sidecars for the matching page_id.

    Returns the absolute Path to the blob .txt file, or None if not a note ref or not found.
    """
    m = _NOTE_PATH_RE.match(path.strip())
    if not m:
        return None

    target_page_id = m.group("page_id").strip()
    blob_idx = m.group("blob_idx")

    if not NOTES_TEXT_DIR.exists():
        return None

    # Scan subdirectories for matching page_id in .page_meta.json
    for page_dir in NOTES_TEXT_DIR.iterdir():
        if not page_dir.is_dir():
            continue
        meta_file = page_dir / ".page_meta.json"
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
                if meta.get("page_id") == target_page_id:
                    txt_file = page_dir / f"{blob_idx}.txt"
                    if txt_file.exists():
                        return txt_file
                    return None
            except (json.JSONDecodeError, OSError):
                continue

    return None


# ── Wildcard/glob detection ───────────────────────────────────────────────────

_GLOB_CHARS_RE = re.compile(r"[*?\[\]]")


def _is_glob_pattern(query: str) -> bool:
    """Return True if query looks like a file glob pattern (e.g. *.py, **/*.txt)."""
    return bool(_GLOB_CHARS_RE.search(query))


# ── Fuzzy filename matching ───────────────────────────────────────────────────

_FUZZY_FILENAME_THRESHOLD = 0.75  # minimum similarity ratio to consider a fuzzy match


def _fuzzy_filename_match(query: str, filename: str) -> float:
    """
    Score how well *query* matches *filename* using fuzzy logic.
    Returns a similarity score in [0, 1]. Higher is better.

    Handles cases like:
      - "mathHelper.py" → "mathHelpers.py"  (missing plural 's')
      - "mathHelper" → "mathHelpers.py"     (query without extension)
      - "server" → "server.py"             (stem-only query)

    A score >= _FUZZY_FILENAME_THRESHOLD is considered a match.
    """
    q = query.lower()
    f = filename.lower()

    # Exact substring — already handled by the caller, but just in case
    if q in f:
        return 1.0

    # Compare query stem vs filename stem (ignore extensions for matching)
    q_stem = Path(q).stem
    f_stem = Path(f).stem
    q_ext = Path(q).suffix
    f_ext = Path(f).suffix

    # If query has an extension and it doesn't match, reduce score
    ext_penalty = 0.0
    if q_ext and f_ext and q_ext != f_ext:
        ext_penalty = 0.15

    # Primary: compare stems using SequenceMatcher
    stem_ratio = SequenceMatcher(None, q_stem, f_stem).ratio()

    # Bonus: if one stem contains the other as a substring
    if q_stem in f_stem or f_stem in q_stem:
        stem_ratio = max(stem_ratio, 0.85)

    return max(0.0, stem_ratio - ext_penalty)


def _list_files_by_glob(pattern: str) -> list[types.TextContent]:
    """
    List workspace files matching a glob/wildcard pattern.
    Supports patterns like *.py, **/*.txt, test_*, etc.
    """
    log.info("list_files_by_glob | pattern=%s", pattern)
    # Normalize: strip leading ./ or /
    pattern = pattern.lstrip("./\\")

    lines = []
    for root, dirs, files in os.walk(WORKSPACE_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        level = Path(root).relative_to(WORKSPACE_ROOT)
        prefix = str(level) if str(level) != "." else ""

        for f in files:
            rel_path = f if not prefix else f"{prefix}/{f}"
            abs_path = Path(root) / f

            if abs_path.is_symlink():
                continue
            if _is_sensitive(f, rel_path):
                continue

            # Match against filename only, or full relative path depending on pattern
            if "/" in pattern or "\\" in pattern:
                # Pattern includes path separators — match against full rel_path
                if fnmatch.fnmatch(rel_path.lower(), pattern.lower()):
                    lines.append(f"  {rel_path}")
            else:
                # Pattern is filename-only — match just the filename
                if fnmatch.fnmatch(f.lower(), pattern.lower()):
                    lines.append(f"  {rel_path}")

    if not lines:
        return [types.TextContent(type="text", text=f"No files matching '{pattern}'.")]

    header = f"Files matching '{pattern}':\n"
    return [types.TextContent(type="text", text=header + "\n".join(lines))]


def _read_scannable(path: Path, max_bytes: int = MAX_SCAN_BYTES) -> list[str] | None:
    """One open(): size cap, null-byte detection, line streaming. Returns lines or None."""
    try:
        with path.open("rb") as f:
            chunk = f.read(max_bytes + 1)
    except OSError:
        return None
    if len(chunk) > max_bytes or b"\x00" in chunk[:8192]:
        return None
    return chunk.decode(errors="replace").splitlines()


# ── list_files ────────────────────────────────────────────────────────────────

async def _list_files(topic: str | None = None) -> list[types.TextContent]:
    log.info("list_files | listing workspace root | topic=%s", topic)
    topic_lower = topic.lower() if topic else None
    lines = []

    for root, dirs, files in os.walk(WORKSPACE_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        level = Path(root).relative_to(WORKSPACE_ROOT)
        prefix = str(level) if str(level) != "." else ""
        section_header = f"{prefix}/" if prefix else "/"
        section_lines = []

        for f in files:
            abs_path = Path(root) / f
            rel_path = f if not prefix else f"{prefix}/{f}"

            # Symlink guard (files — dirs are handled by dirs[:] filter above)
            if abs_path.is_symlink():
                continue

            # No topic filter — just list everything (still skip sensitives)
            if topic_lower is None:
                if not _is_sensitive(f, rel_path):
                    section_lines.append(f"  {rel_path}")
                continue

            # Topic set: filename match (exact substring)
            if topic_lower in f.lower():
                section_lines.append(f"  {rel_path}  [match: filename]")
                continue

            # Topic set: filename match (fuzzy — catches typos, missing plurals, etc.)
            fuzzy_score = _fuzzy_filename_match(topic_lower, f)
            if fuzzy_score >= _FUZZY_FILENAME_THRESHOLD:
                section_lines.append(f"  {rel_path}  [match: filename (fuzzy)]")
                continue

            # Topic set: content match (only for scannable files)
            if not _is_scannable(abs_path, rel_path):
                continue
            file_lines = _read_scannable(abs_path)
            if file_lines is None:
                continue
            hit_ranges = _find_topic_ranges(file_lines, topic_lower, context=2)

            if hit_ranges:
                range_strs = ", ".join(f"{s}-{e}" for s, e in hit_ranges)
                section_lines.append(f"  {rel_path}  [match: lines {range_strs}]")

        if section_lines:
            lines.append(section_header)
            lines.extend(section_lines)

    return [types.TextContent(type="text", text="\n".join(lines) or "No files found.")]


# ── read_file ─────────────────────────────────────────────────────────────────

async def _read_file(path: str, start: int = 1, count: int | None = None) -> list[types.TextContent]:
    log.info("read_file | path=%s start=%s count=%s", path, start, count)

    # ── Handle note-style paths (e.g. "note: page_id / blob N") ──────────────
    note_target = _resolve_note_path(path)
    if note_target is not None:
        try:
            text = note_target.read_text(encoding="utf-8")
        except OSError as e:
            return [types.TextContent(type="text", text=f"Error reading note blob: {e}")]
        rel = note_target.relative_to(NOTES_TEXT_DIR)
        header = f"Note content ({rel}):\n\n"
        return [types.TextContent(type="text", text=header + text)]
    elif _NOTE_PATH_RE.match(path.strip()):
        # It looked like a note reference but we couldn't resolve it
        return [types.TextContent(type="text", text=f"Note not found: {path}. The page may have been deleted or renamed.")]

    raw = WORKSPACE_ROOT / path
    if _has_symlink_component(raw):
        return [types.TextContent(type="text", text="Access denied: symlinks not allowed.")]
    target = raw.resolve()

    try:
        target.relative_to(WORKSPACE_ROOT)
    except ValueError:
        return [types.TextContent(type="text", text="Access denied.")]

    if target.is_symlink():
        return [types.TextContent(type="text", text="Access denied: symlinks not allowed.")]

    if _is_sensitive(target.name, path):
        return [types.TextContent(type="text", text="Access denied: sensitive file.")]

    try:
        size = target.stat().st_size
    except FileNotFoundError:
        return [types.TextContent(type="text", text=f"File not found: {path}. Include the file extension (e.g. 'draft.txt' not 'draft'). Try search_workspace with '*' to list all files.")]

    with target.open("rb") as f:
        raw_bytes = f.read(MAX_SCAN_BYTES)
    truncated_by_size = size > MAX_SCAN_BYTES
    all_lines = raw_bytes.decode(errors="replace").splitlines()
    total = len(all_lines)

    if total == 0:
        return [types.TextContent(type="text", text=f"{path}: empty file.")]
    if count is not None and count <= 0:
        return [types.TextContent(type="text", text="count must be > 0.")]

    start = max(1, min(start, total))
    end = (start + count - 1) if count is not None else total
    end = min(end, start + MAX_FILE_LINES - 1, total)

    selected = all_lines[start - 1 : end]
    was_clamped = end < total and (count is None or start + count - 1 > end)

    if truncated_by_size:
        header = f"Contents of {path} (lines {start}-{end}, file exceeds size limit — prefix only):\n\n"
    else:
        header = f"Contents of {path} (lines {start}-{end} of {total}):\n\n"

    content = "\n".join(selected)

    if was_clamped or truncated_by_size:
        content += f"\n\n[... truncated at line {end} of {total}{'+ (file exceeds size limit)' if truncated_by_size else ''}]"

    return [types.TextContent(type="text", text=header + content)]


# ── read_code_skeleton ────────────────────────────────────────────────────────

async def _read_code_skeleton(path: str) -> list[types.TextContent]:
    log.info("read_code_skeleton | path=%s", path)
    raw = WORKSPACE_ROOT / path
    if _has_symlink_component(raw):
        return [types.TextContent(type="text", text="Access denied: symlinks not allowed.")]
    target = raw.resolve()

    try:
        target.relative_to(WORKSPACE_ROOT)
    except ValueError:
        return [types.TextContent(type="text", text="Access denied.")]

    if target.is_symlink():
        return [types.TextContent(type="text", text="Access denied: symlinks not allowed.")]

    if _is_sensitive(target.name, path):
        return [types.TextContent(type="text", text="Access denied: sensitive file.")]

    try:
        target.stat().st_size
    except FileNotFoundError:
        return [types.TextContent(type="text", text=f"File not found: {path}. Include the file extension (e.g. 'draft.txt' not 'draft'). Try search_workspace with '*' to list all files.")]

    try:
        with target.open("r", encoding="utf-8", errors="replace") as f:
            source = f.read()
    except Exception as e:
        return [types.TextContent(type="text", text=f"Error reading file: {e}")]

    ext = target.suffix.lower()
    if ext == ".py":
        skeleton = _get_python_skeleton(source, path)
    else:
        skeleton = _get_generic_skeleton(source, path)

    return [types.TextContent(type="text", text=skeleton)]


def _get_python_symbols(source_code: str):
    """Extract classes and functions with their metadata."""
    try:
        tree = ast.parse(source_code)
        symbols = []

        def get_func_info(node):
            is_async = isinstance(node, ast.AsyncFunctionDef)
            stub_kwargs = {
                "name": node.name, "args": node.args,
                "body": [ast.Pass()], "decorator_list": [], "returns": node.returns
            }
            stub = ast.AsyncFunctionDef(**stub_kwargs) if is_async else ast.FunctionDef(**stub_kwargs)
            try:
                sig = ast.unparse(stub).replace("\n    pass", " ...")
            except AttributeError:
                sig = f"{'async def ' if is_async else 'def '}{node.name}(...): ..."
            
            doc = ast.get_docstring(node)
            first_doc = doc.strip().split("\n")[0] if doc else None
            
            return {
                "name": node.name,
                "type": "function",
                "start_line": node.lineno,
                "end_line": getattr(node, "end_lineno", node.lineno),
                "signature": sig,
                "docstring": first_doc
            }

        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                doc = ast.get_docstring(node)
                first_doc = doc.strip().split("\n")[0] if doc else None
                cls_symbol = {
                    "name": node.name,
                    "type": "class",
                    "start_line": node.lineno,
                    "end_line": getattr(node, "end_lineno", node.lineno),
                    "docstring": first_doc,
                    "children": []
                }
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        cls_symbol["children"].append(get_func_info(item))
                symbols.append(cls_symbol)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols.append(get_func_info(node))
        
        return symbols
    except (SyntaxError, Exception):
        return None

def _get_generic_symbols(source_code: str):
    """Extract symbol-like lines using heuristics and brace-counting for blocks."""
    lines = source_code.splitlines()
    symbols = []
    
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped or len(line) > 200:
            continue
            
        # Skip control flow structures
        if stripped.startswith(("if ", "if(", "for ", "for(", "while ", "while(",
                                "switch ", "switch(", "else", "catch ", "catch(", "try")):
            continue
        if stripped == "}" or stripped == "};":
            continue

        symbol_type = None
        if any(stripped.startswith(kw) for kw in ["import ", "require(", "using ", "include ", "from "]):
            symbol_type = "import"
        elif any(f" {kw} " in line for kw in ["class ", "interface ", "struct ", "enum ", "trait "]):
            symbol_type = "class"
        elif "(" in line and ")" in line and "{" in line:
            symbol_type = "function"
            
        if symbol_type:
            # Find end line by brace counting if it's a block
            end_line = i + 1
            if "{" in line:
                braces = 0
                for j in range(i, len(lines)):
                    braces += lines[j].count("{")
                    braces -= lines[j].count("}")
                    if braces <= 0:
                        end_line = j + 1
                        break
            
            symbols.append({
                "name": line.strip().split("(")[0].split()[-1] if symbol_type != "import" else "import",
                "type": symbol_type,
                "start_line": i + 1,
                "end_line": end_line,
                "content": line
            })
            
    return symbols

def _get_python_skeleton(source_code: str, file_path: str) -> str:
    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        return f"Error: {file_path} contains invalid Python syntax."
    except Exception as e:
        return f"Error parsing {file_path}: {e}"

    symbols = _get_python_symbols(source_code)
    skeleton = [f"### Skeleton for: {file_path} ###\n"]
    
    # Handle imports separately to preserve skeleton output as requested
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            try:
                skeleton.append(ast.unparse(node))
            except AttributeError:
                pass

    if symbols:
        for s in symbols:
            if s["type"] == "class":
                skeleton.append(f"\nclass {s['name']}:")
                if s["docstring"]:
                    skeleton.append(f'    """{s["docstring"]}..."""')
                for child in s["children"]:
                    skeleton.append(f"    {child['signature']}")
                    if child["docstring"]:
                        skeleton.append(f'        """{child["docstring"]}..."""')
            elif s["type"] == "function":
                skeleton.append(s["signature"])
                if s["docstring"]:
                    skeleton.append(f'    """{s["docstring"]}..."""')
    
    return "\n".join(skeleton)

def _get_generic_skeleton(source_code: str, file_path: str) -> str:
    symbols = _get_generic_symbols(source_code)
    skeleton = [f"### Skeleton for: {file_path} ###\n"]
    
    for s in symbols:
        skeleton.append(f"{s['start_line']}: {s['content']}")
        
    if len(skeleton) == 1:
        return f"### Skeleton for: {file_path} ###\n(No classes or functions detected)"

    return "\n".join(skeleton)

_CHUNK_TARGET_LINES = 30   # soft target for paragraph windows
_CHUNK_MAX_LINES    = 60   # hard ceiling before we force a split


def _chunk_markdown(source: str, path: str) -> list[dict]:
    """
    Split a markdown file on heading boundaries (# / ## / ### …).
    Each section from one heading to the next becomes one chunk.
    A leading block before the first heading gets its own chunk too.
    """
    lines = source.splitlines()
    chunks: list[dict] = []
    section_start = 0
    section_title = "preamble"

    def _flush(start: int, end: int, title: str, idx: int) -> None:
        block = lines[start:end]
        text = "\n".join(block).strip()
        if not text:
            return
        slug = title.replace(" ", "_")[:40]
        chunks.append({
            "id": f"{path}::{idx}_{slug}",
            "file": path,
            "type": "section",
            "start_line": start + 1,
            "count": end - start,
            "code": text,
        })

    for i, line in enumerate(lines):
        if line.startswith("#") and i > section_start:
            _flush(section_start, i, section_title, len(chunks))
            section_start = i
            section_title = line.lstrip("#").strip() or f"section_{i}"

    _flush(section_start, len(lines), section_title, len(chunks))
    return chunks


def _chunk_paragraphs(source: str, path: str) -> list[dict]:
    """
    Split plain-text / HTML on blank lines, then merge small paragraphs into
    windows of up to _CHUNK_TARGET_LINES lines so we don't index tiny stubs.
    """
    lines = source.splitlines()
    # Collect paragraph spans [start, end) (0-indexed)
    paragraphs: list[tuple[int, int]] = []
    start = 0
    in_para = False

    for i, line in enumerate(lines):
        if line.strip():
            if not in_para:
                start = i
                in_para = True
        else:
            if in_para:
                paragraphs.append((start, i))
                in_para = False
    if in_para:
        paragraphs.append((start, len(lines)))

    if not paragraphs:
        return [{
            "id": path,
            "file": path,
            "type": "file",
            "start_line": 1,
            "count": len(lines),
            "code": source.strip(),
        }]

    chunks: list[dict] = []
    window_start, window_end = paragraphs[0]

    for para_start, para_end in paragraphs[1:]:
        current_size = window_end - window_start
        next_size = para_end - para_start

        if current_size + next_size <= _CHUNK_TARGET_LINES:
            # Merge into current window
            window_end = para_end
        else:
            # Flush current window
            text = "\n".join(lines[window_start:window_end]).strip()
            if text:
                chunks.append({
                    "id": f"{path}::{len(chunks)}",
                    "file": path,
                    "type": "passage",
                    "start_line": window_start + 1,
                    "count": window_end - window_start,
                    "code": text,
                })
            # If next paragraph alone exceeds max, split it into fixed windows
            if next_size > _CHUNK_MAX_LINES:
                for off in range(para_start, para_end, _CHUNK_TARGET_LINES):
                    chunk_end = min(off + _CHUNK_TARGET_LINES, para_end)
                    text = "\n".join(lines[off:chunk_end]).strip()
                    if text:
                        chunks.append({
                            "id": f"{path}::{len(chunks)}",
                            "file": path,
                            "type": "passage",
                            "start_line": off + 1,
                            "count": chunk_end - off,
                            "code": text,
                        })
                window_start, window_end = para_end, para_end
            else:
                window_start, window_end = para_start, para_end

    # Flush remaining window
    if window_start < window_end:
        text = "\n".join(lines[window_start:window_end]).strip()
        if text:
            chunks.append({
                "id": f"{path}::{len(chunks)}",
                "file": path,
                "type": "passage",
                "start_line": window_start + 1,
                "count": window_end - window_start,
                "code": text,
            })

    return chunks


def get_file_chunks(path: str) -> list[dict]:
    """Extract chunks for a file, applying all security and filtering guards."""
    raw = WORKSPACE_ROOT / path
    if _has_symlink_component(raw):
        return []
    target = raw.resolve()
    
    try:
        target.relative_to(WORKSPACE_ROOT)
    except ValueError:
        return []
        
    if target.is_symlink() or not target.is_file():
        return []

    # Check SKIP_DIRS for all parent components
    rel_path = target.relative_to(WORKSPACE_ROOT)
    for part in rel_path.parts[:-1]:
        if part in SKIP_DIRS:
            return []

    if _is_sensitive(target.name, str(rel_path)):
        return []

    ext = target.suffix.lower()
    code_exts = (".js", ".ts", ".jsx", ".tsx", ".java", ".cpp", ".c", ".cs")
    
    if ext not in SCANNABLE_EXTENSIONS and ext not in code_exts:
        return []

    try:
        with target.open("r", encoding="utf-8", errors="replace") as f:
            source = f.read()
    except Exception:
        return []

    ext = target.suffix.lower()
    chunks = []
    
    if ext == ".py":
        symbols = _get_python_symbols(source)
        if symbols:
            for s in symbols:
                if s["type"] in ("function", "class"):
                    # For class, we want the whole block
                    lines = source.splitlines()
                    code = "\n".join(lines[s["start_line"]-1 : s["end_line"]])
                    chunks.append({
                        "id": f"{path}::{s['name']}",
                        "file": path,
                        "type": s["type"],
                        "start_line": s["start_line"],
                        "count": s["end_line"] - s["start_line"] + 1,
                        "code": code
                    })
        else:
            log.warning(f"Failed to parse {path}, skipping.")
    elif ext in (".js", ".ts", ".jsx", ".tsx", ".java", ".cpp", ".c", ".cs"):
        symbols = _get_generic_symbols(source)
        lines = source.splitlines()
        for s in symbols:
            if s["type"] in ("function", "class"):
                code = "\n".join(lines[s["start_line"]-1 : s["end_line"]])
                chunks.append({
                    "id": f"{path}::{s['name']}",
                    "file": path,
                    "type": s["type"],
                    "start_line": s["start_line"],
                    "count": s["end_line"] - s["start_line"] + 1,
                    "code": code
                })
    elif ext == ".md":
        chunks = _chunk_markdown(source, path)
    elif ext in (".txt", ".html"):
        chunks = _chunk_paragraphs(source, path)
    else:
        # YAML, TOML, JSON, CSS, shell, etc. — usually small; single chunk is fine
        lines = source.splitlines()
        chunks.append({
            "id": path,
            "file": path,
            "type": "file",
            "start_line": 1,
            "count": len(lines),
            "code": source
        })
        
    return chunks
