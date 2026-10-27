"""
tool_apps/notes/winink_recognizer.py — Windows Ink recognition wrapper.

Drives the native C# Windows Ink CLI tool (winink/InkRecognizer) via subprocess,
parses its JSON output, and writes ONLY the recognized page text into the notes
text folder, keeping that tree clean for the assistant to search/read:

    aiWorkspace/notesAppText/<title-slug>/
        0.txt            plain recognized page text (regions joined by blank lines)

All recognition bookkeeping (the source-strokes skip-gate hash and the
page_id -> slug reverse map) lives OUTSIDE that tree, in the notes app's private
data dir, via tool_apps/notes/recognition_state.py:

    tool_apps/notes/data/<type>/_recognition.json
        { "<page_id>": {"slug": "<title-slug>", "hash": "<strokes-sha1>"} }

This replaces the retired image-OCR pipeline in tool_apps/notes/ocr/. We write a
single file (0.txt) per page because Windows Ink already does spatial grouping
internally; the whole page's recognized text goes into one file. 0.txt is picked
up by the normal mtime-gated workspace indexer like any other text file.

process_page_winink() never raises — recognition failures are logged and skipped.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
from pathlib import Path

from tool_apps.notes import recognition_state

log = logging.getLogger(__name__)

# ── Paths ───────────────────────────────────────────────────────────────────────

# tool_apps/notes/winink_recognizer.py  ->  workspace root is three parents up.
_WORKSPACE_ROOT = Path(__file__).resolve().parent.parent.parent

# Stable published exe path (see winink/build.ps1).
INK_RECOGNIZER_EXE = _WORKSPACE_ROOT / "winink" / "bin" / "InkRecognizer.exe"

# Notes data root — used to resolve meta.json for the title-slug output dir.
_NOTES_DATA_ROOT = Path(__file__).resolve().parent / "data"

# Subprocess timeout (seconds). Recognition is fast; this is just a safety cap.
_RECOGNIZE_TIMEOUT_SECS = 60.0


# ── Title-slug resolution (mirrors the retired pipeline so output dirs match) ─────

def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "untitled"


def _slug_for_page(page_id: str, page_type: str) -> str:
    """
    Human-readable slug for a page: its title (slugified) or, failing that,
    the slug of its id. Resolved against the page's own type directory so a
    page_id that happens to exist in both 'notes' and 'art' uses the right
    title.
    """
    meta_path = _NOTES_DATA_ROOT / page_type / page_id / "meta.json"
    if meta_path.exists():
        try:
            with meta_path.open("r", encoding="utf-8") as f:
                meta = json.load(f)
            title = (meta.get("title") or "").strip()
            if title and title.lower() != "untitled":
                return _slugify(title)
        except Exception:
            pass
    return _slugify(page_id)


def _slug_owner(slug: str, page_type: str) -> str | None:
    """Return the page_id that currently owns an output-dir slug, via the map."""
    return recognition_state.slug_owner(page_type, slug)


def _exists_in_both_types(page_id: str) -> bool:
    """True when the same page_id directory exists under both notes/ and art/."""
    return (
        (_NOTES_DATA_ROOT / "notes" / page_id).is_dir()
        and (_NOTES_DATA_ROOT / "art" / page_id).is_dir()
    )


def _page_output_dir(page_id: str, page_type: str, output_root: Path) -> Path:
    """
    Return the output directory for a page's recognized text.

    Uses the title slug for readability. Disambiguates by appending the type
    when either:
      - the same page_id exists under both notes/ and art/ (so the two type
        variants never share a 0.txt), or
      - the natural slug dir is already owned by a different page_id
        (ownership is tracked in the recognition map, not on disk).
    """
    slug = _slug_for_page(page_id, page_type)

    # Same page_id in both type dirs: always keep them in separate, typed dirs.
    if _exists_in_both_types(page_id):
        return output_root / _slugify(f"{slug}-{page_type}")

    owner = _slug_owner(slug, page_type)
    if owner is None or owner == page_id:
        return output_root / slug

    # Collision with a different page_id — suffix with type (then id if needed).
    typed_slug = _slugify(f"{slug}-{page_type}")
    typed_owner = _slug_owner(typed_slug, page_type)
    if typed_owner is None or typed_owner == page_id:
        return output_root / typed_slug
    return output_root / _slugify(f"{slug}-{page_type}-{page_id}")


# ── Content hashing (skip-on-unchanged) ───────────────────────────────────────────

def _hash_source(gz_path: Path) -> str:
    """SHA1 of the raw strokes file bytes — a stable fingerprint of the page."""
    h = hashlib.sha1()
    with gz_path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _remove_stale_blobs(page_out: Path) -> None:
    """
    Delete leftover N.txt files with N >= 1 in a page's output dir, plus any
    legacy .hash / .page_meta.json sidecars from the retired layout.

    Windows Ink writes a single file (0.txt). The retired multi-blob pipeline
    could leave 1.txt, 2.txt, ... behind, and older runs wrote 0.hash /
    .page_meta.json sidecars we no longer use; all of those would otherwise
    linger in the agent-visible notes text tree.
    """
    for txt in page_out.glob("*.txt"):
        try:
            idx = int(txt.stem)
        except ValueError:
            continue
        if idx <= 0:
            continue
        try:
            txt.unlink()
        except OSError:
            pass
        log.info("winink: removed stale blob file %s", txt.name)

    # Legacy sidecars that must never remain in the clean text tree.
    for legacy in (*page_out.glob("*.hash"), page_out / ".page_meta.json"):
        try:
            legacy.unlink()
        except OSError:
            pass


# ── CLI invocation ────────────────────────────────────────────────────────────────

def _run_recognizer(gz_path: Path) -> dict | None:
    """
    Invoke the Windows Ink CLI on a strokes.json.gz path and return the parsed
    JSON dict, or None on any failure (logged, never raised).
    """
    if not INK_RECOGNIZER_EXE.exists():
        log.error(
            "winink: recognizer exe not found at %s — run winink/build.ps1",
            INK_RECOGNIZER_EXE,
        )
        return None

    try:
        proc = subprocess.run(
            [str(INK_RECOGNIZER_EXE), str(gz_path)],
            capture_output=True,
            text=True,
            timeout=_RECOGNIZE_TIMEOUT_SECS,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        log.error("winink: recognition timed out for %s", gz_path)
        return None
    except Exception as exc:
        log.error("winink: failed to launch recognizer for %s: %s", gz_path, exc)
        return None

    stdout = (proc.stdout or "").strip()
    if not stdout:
        log.error(
            "winink: empty output for %s (exit=%s, stderr=%s)",
            gz_path, proc.returncode, (proc.stderr or "").strip()[:200],
        )
        return None

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        log.error("winink: non-JSON output for %s: %s", gz_path, exc)
        return None

    if isinstance(data, dict) and data.get("error"):
        log.error("winink: recognizer error for %s: %s", gz_path, data["error"])
        return None

    return data


def _normalize_text(page_text: str) -> str:
    """Normalize CRLF/CR line endings from the Windows CLI to plain LF."""
    return page_text.replace("\r\n", "\n").replace("\r", "\n")


# ── Main entry point ───────────────────────────────────────────────────────────────

def process_page_winink(
    page_id: str,
    gz_path: Path | str,
    output_dir: Path,
    page_type: str = "notes",
) -> None:
    """
    Recognize one page via Windows Ink and write only 0.txt into
    output_dir/<title-slug>/. The source-strokes hash and the slug are recorded
    in the notes app's private recognition map (recognition_state), never in the
    text tree.

    Incremental: if the source-strokes hash matches the hash stored in the map,
    the page is skipped. Never raises — failures are logged.

    page_type ("notes" or "art") disambiguates the output dir when a page_id
    exists in both type directories.
    """
    gz_path = Path(gz_path)
    if not gz_path.exists():
        log.warning("winink: strokes file missing for page %s: %s", page_id, gz_path)
        return

    page_out = _page_output_dir(page_id, page_type, output_dir)
    out_txt = page_out / "0.txt"

    # Incremental skip: unchanged source strokes -> nothing to do. We still
    # require the text file to exist, so a cleared text tree re-recognises.
    content_hash = _hash_source(gz_path)
    if recognition_state.get_hash(page_type, page_id) == content_hash and out_txt.exists():
        log.debug("winink: page %s unchanged, skipping.", page_id)
        return

    data = _run_recognizer(gz_path)
    if data is None:
        return  # already logged

    page_text = _normalize_text(str(data.get("page_text") or "")).strip()

    page_out.mkdir(parents=True, exist_ok=True)

    # Plain recognized text — the only thing we put in the agent-visible tree.
    try:
        out_txt.write_text(page_text, encoding="utf-8")
    except Exception as exc:
        log.error("winink: failed writing output for page %s: %s", page_id, exc)
        return

    # Record the slug + source hash in the notes-app-private map (skip-gate +
    # reverse lookup for delete/rename).
    try:
        recognition_state.set_entry(page_type, page_id, page_out.name, content_hash)
    except Exception as exc:
        log.warning("winink: failed updating recognition map for %s: %s", page_id, exc)

    # Windows Ink produces a single file (index 0) per page. Remove any leftover
    # 1.txt, 2.txt, ... from the retired multi-blob pipeline and any legacy
    # .hash / .page_meta.json sidecars, so the text tree stays clean.
    _remove_stale_blobs(page_out)

    region_count = len(data.get("regions") or [])
    log.info(
        "winink: page %s → %d region(s), %d chars → %s/0.txt",
        page_id, region_count, len(page_text), page_out.name,
    )
