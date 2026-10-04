"""
tool_apps/notes/winink_recognizer.py — Windows Ink recognition wrapper.

Drives the native C# Windows Ink CLI tool (winink/InkRecognizer) via subprocess,
parses its JSON output, and writes the recognized page text into the notes text
folder in the layout the indexer (tools/index.py scan_note_blobs) expects:

    aiWorkspace/notesAppText/<title-slug>/
        0.txt            plain recognized page text (regions joined by blank lines)
        0.hash           content fingerprint of the source strokes (skip-on-unchanged)
        .page_meta.json  {"page_id": "..."} so the indexer can recover the page id

This replaces the retired image-OCR pipeline in tool_apps/notes/ocr/. We write a
single blob (index 0) per page because Windows Ink already does spatial grouping
internally; the whole page's recognized text goes into one file.

process_page_winink() never raises — recognition failures are logged and skipped.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
from pathlib import Path

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


def _dir_owner(page_out: Path) -> str | None:
    """Return the page_id that currently owns an output dir, via its sidecar."""
    sidecar = page_out / ".page_meta.json"
    if not sidecar.exists():
        return None
    try:
        return json.loads(sidecar.read_text(encoding="utf-8")).get("page_id")
    except Exception:
        return None


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
        variants never share a 0.txt / 0.hash), or
      - the natural slug dir is already owned by a different page_id.
    """
    slug = _slug_for_page(page_id, page_type)

    # Same page_id in both type dirs: always keep them in separate, typed dirs.
    if _exists_in_both_types(page_id):
        return output_root / _slugify(f"{slug}-{page_type}")

    candidate = output_root / slug
    owner = _dir_owner(candidate)
    if owner is None or owner == page_id:
        return candidate

    # Collision with a different page_id — suffix with type (then id if needed).
    typed = output_root / _slugify(f"{slug}-{page_type}")
    typed_owner = _dir_owner(typed)
    if typed_owner is None or typed_owner == page_id:
        return typed
    return output_root / _slugify(f"{slug}-{page_type}-{page_id}")


# ── Content hashing (skip-on-unchanged) ───────────────────────────────────────────

def _hash_source(gz_path: Path) -> str:
    """SHA1 of the raw strokes file bytes — a stable fingerprint of the page."""
    h = hashlib.sha1()
    with gz_path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_stored_hash(txt_path: Path) -> str | None:
    try:
        return txt_path.with_suffix(".hash").read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _remove_stale_blobs(page_out: Path) -> None:
    """
    Delete leftover N.txt / N.hash files with N >= 1 in a page's output dir.

    Windows Ink writes a single blob (0.txt). The retired multi-blob pipeline
    could leave 1.txt, 2.txt, ... behind; those would otherwise linger as
    phantom blobs. (scan_note_blobs prunes the matching DB rows.)
    """
    for txt in page_out.glob("*.txt"):
        try:
            idx = int(txt.stem)
        except ValueError:
            continue
        if idx <= 0:
            continue
        for f in (txt, txt.with_suffix(".hash")):
            try:
                f.unlink()
            except OSError:
                pass
        log.info("winink: removed stale blob file %s", txt.name)


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
    Recognize one page via Windows Ink and write 0.txt / 0.hash / .page_meta.json
    into output_dir/<title-slug>/.

    Incremental: if the source-strokes hash matches the stored .hash sidecar, the
    page is skipped. Never raises — failures are logged.

    page_type ("notes" or "art") disambiguates the output dir when a page_id
    exists in both type directories.
    """
    gz_path = Path(gz_path)
    if not gz_path.exists():
        log.warning("winink: strokes file missing for page %s: %s", page_id, gz_path)
        return

    page_out = _page_output_dir(page_id, page_type, output_dir)
    out_txt = page_out / "0.txt"

    # Incremental skip: unchanged source strokes -> nothing to do.
    content_hash = _hash_source(gz_path)
    if _read_stored_hash(out_txt) == content_hash:
        log.debug("winink: page %s unchanged, skipping.", page_id)
        return

    data = _run_recognizer(gz_path)
    if data is None:
        return  # already logged

    page_text = _normalize_text(str(data.get("page_text") or "")).strip()

    page_out.mkdir(parents=True, exist_ok=True)

    # .page_meta.json so the indexer can recover the real page_id from the slug dir.
    meta_sidecar = page_out / ".page_meta.json"
    try:
        meta_sidecar.write_text(json.dumps({"page_id": page_id}), encoding="utf-8")
    except Exception as exc:
        log.warning("winink: failed writing page meta for %s: %s", page_id, exc)

    # Plain text + hash sidecar (same shape the indexer expects for a blob).
    try:
        out_txt.write_text(page_text, encoding="utf-8")
        out_txt.with_suffix(".hash").write_text(content_hash, encoding="utf-8")
    except Exception as exc:
        log.error("winink: failed writing output for page %s: %s", page_id, exc)
        return

    # Windows Ink produces a single blob (index 0) per page. Older runs of the
    # retired multi-blob pipeline may have left 1.txt, 2.txt, ... behind in this
    # directory; remove them (and their .hash sidecars) so the indexer doesn't
    # keep embedding stale phantom blobs. The matching DB rows are cleared by
    # scan_note_blobs when their files disappear.
    _remove_stale_blobs(page_out)

    region_count = len(data.get("regions") or [])
    log.info(
        "winink: page %s → %d region(s), %d chars → %s/0.txt",
        page_id, region_count, len(page_text), page_out.name,
    )
