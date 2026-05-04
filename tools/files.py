"""
tools/files.py — Workspace file tool implementations.
Provides list_files, read_file, and read_code_skeleton with strict
path-traversal and symlink guards.
"""
import os
import ast
import logging
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
    "dist", "build", ".mypy_cache", ".pytest_cache", ".index",
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

            # Topic set: filename match
            if topic_lower in f.lower():
                section_lines.append(f"  {rel_path}  [match: filename]")
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
        return [types.TextContent(type="text", text=f"File not found: {path}")]

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
        return [types.TextContent(type="text", text=f"File not found: {path}")]

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
    else:
        # Non-code or other files: single chunk
        lines = source.splitlines()
        chunks.append({
            "id": f"{path}",
            "file": path,
            "type": "file",
            "start_line": 1,
            "count": len(lines),
            "code": source
        })
        
    return chunks
