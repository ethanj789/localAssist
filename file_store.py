"""File storage for chat message attachments.

Generalizes the older image-only store into a per-file store. Every uploaded
file (PDF now, images as the degenerate one-page case, more types later) is
rendered once at upload into:

  * rendered page PNGs (``<file_id>/1.png``, ``2.png`` ... — 1-indexed, in
    page order),
  * a page-labeled extracted-text sidecar (``<file_id>/text.txt`` — only when
    the file had an extractable text layer),
  * the original bytes, kept for a future download feature (``raw/<file_id>.<ext>``).

On-disk layout (one folder per conversation, so delete/prune is a single
``rmtree``)::

    chat_files/
      <conversation_id>/
        raw/
          <file_id>.pdf          # original bytes
          <file_id>.png
        <file_id>/
          1.png  2.png ...       # rendered pages, page order
          text.txt               # page-labeled text (only if hasText)

The ``messages.files`` DB column holds only lean *file objects* (see ``db.py``)::

    { "type": "pdf",   "path": "<file_id>", "name": "report.pdf", "pages": 5, "hasText": true  }
    { "type": "image", "path": "<file_id>", "name": "shot.png",   "pages": 1, "hasText": false }

Three representations stay strictly separated, exactly as in the old image
pipeline:

  * **data URL** (``data:<type>;base64,<b64>``): browser side only — what the
    composer holds and what ``POST /chat`` carries.
  * **relative path** (``<conv_id>/<file_id>/1.png``): DB + ``/chat-file/`` URLs
    + what the read path returns.
  * **bare base64** (no prefix): only what Ollama receives in per-message
    ``images: [...]``.
"""

from __future__ import annotations

import base64
import io
import logging
import re
import shutil
import uuid
from pathlib import Path

from config import ROOT_DIR

log = logging.getLogger(__name__)

FILES_DIR = ROOT_DIR / "chat_files"

# Guard against pathological uploads (~15 MB of base64 ~= 11 MB decoded).
MAX_B64_CHARS = 20_000_000

# Hard cap on rendered pages per file; a truncation note is shown to the model
# when a file exceeds this (handled in prep_file).
MAX_PAGES = 5

# data:<mime>;base64,<payload>  →  capture mime + payload. Deliberately NOT
# image-only: PDFs arrive as data:application/pdf;base64,...
_DATA_URL_RE = re.compile(
    r"^data:(?P<mime>[a-zA-Z0-9.+/-]+);base64,(?P<b64>.+)$", re.DOTALL
)


def _sanitize_component(value: str, fallback: str = "misc") -> str:
    """Reduce a path component (conversation id / file id) to the characters
    valid in our uuid-based ids, guarding against path traversal."""
    return re.sub(r"[^A-Za-z0-9_-]", "", value or "") or fallback


def _conv_dir_path(conversation_id: str) -> Path:
    """Per-conversation directory path (does NOT create it)."""
    return FILES_DIR / _sanitize_component(conversation_id)


def new_file_id() -> str:
    """A fresh per-upload file id (uuid hex). Name collisions are impossible;
    the human-facing filename lives in the DB object, not on disk."""
    return uuid.uuid4().hex


# --------------------------------------------------------------------------- #
# Data-URL parsing
# --------------------------------------------------------------------------- #

def parse_data_url(data_url: str) -> tuple[str, bytes] | None:
    """Split a ``data:<mime>;base64,<payload>`` URL into ``(mime, raw_bytes)``.

    Returns ``None`` when the string isn't a valid base64 data URL or the
    payload is oversized.
    """
    if not isinstance(data_url, str):
        return None
    m = _DATA_URL_RE.match(data_url.strip())
    if not m:
        return None
    b64 = m.group("b64")
    if len(b64) > MAX_B64_CHARS:
        log.warning("file_store: rejecting oversized data URL (%d b64 chars)", len(b64))
        return None
    mime = m.group("mime").lower()
    try:
        raw = base64.b64decode(b64, validate=False)
    except Exception as exc:  # noqa: BLE001
        log.warning("file_store: failed to decode data URL: %s", exc)
        return None
    return mime, raw


# --------------------------------------------------------------------------- #
# Write side (used by prep_file)
# --------------------------------------------------------------------------- #

def file_dir(conversation_id: str, file_id: str) -> Path:
    """Return (and create) the directory holding a file's rendered pages/text."""
    d = _conv_dir_path(conversation_id) / _sanitize_component(file_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def raw_dir(conversation_id: str) -> Path:
    """Return (and create) the per-conversation ``raw/`` directory for originals."""
    d = _conv_dir_path(conversation_id) / "raw"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_original(conversation_id: str, file_id: str, raw: bytes, ext: str) -> str:
    """Persist the original upload bytes under ``raw/<file_id>.<ext>``.

    Returns the stored POSIX relative path (relative to ``FILES_DIR``)."""
    ext = _sanitize_component(ext, fallback="bin").lower()
    fid = _sanitize_component(file_id)
    (raw_dir(conversation_id) / f"{fid}.{ext}").write_bytes(raw)
    return f"{_sanitize_component(conversation_id)}/raw/{fid}.{ext}"


def write_page_png(conversation_id: str, file_id: str, index: int, png_bytes: bytes) -> str:
    """Persist a rendered page PNG as ``<file_id>/<index>.png`` (1-indexed).

    Returns the stored POSIX relative path (relative to ``FILES_DIR``)."""
    fid = _sanitize_component(file_id)
    (file_dir(conversation_id, file_id) / f"{index}.png").write_bytes(png_bytes)
    return f"{_sanitize_component(conversation_id)}/{fid}/{index}.png"


def write_text(conversation_id: str, file_id: str, text: str) -> str:
    """Persist page-labeled extracted text as ``<file_id>/text.txt``.

    Returns the stored POSIX relative path (relative to ``FILES_DIR``)."""
    fid = _sanitize_component(file_id)
    (file_dir(conversation_id, file_id) / "text.txt").write_text(text, encoding="utf-8")
    return f"{_sanitize_component(conversation_id)}/{fid}/text.txt"


# --------------------------------------------------------------------------- #
# Read side (serving bytes + feeding the model)
# --------------------------------------------------------------------------- #

def _resolve(rel_path: str) -> Path | None:
    """Resolve a stored relative path to an absolute path inside ``FILES_DIR``,
    refusing anything that escapes the directory (path-traversal guard)."""
    if not rel_path or not isinstance(rel_path, str):
        return None
    candidate = (FILES_DIR / rel_path).resolve()
    try:
        candidate.relative_to(FILES_DIR.resolve())
    except ValueError:
        log.warning("file_store: refusing path outside FILES_DIR: %r", rel_path)
        return None
    return candidate


def load_bytes(rel_path: str) -> bytes | None:
    """Read raw bytes for a stored relative path (for serving to the UI)."""
    p = _resolve(rel_path)
    if p is None or not p.exists() or not p.is_file():
        return None
    return p.read_bytes()


def load_as_base64(rel_path: str) -> str | None:
    """Read a stored file back as a bare base64 string (no data-URL prefix) —
    the exact shape Ollama's ``/api/chat`` expects in ``images: [...]``."""
    raw = load_bytes(rel_path)
    if raw is None:
        return None
    return base64.b64encode(raw).decode("utf-8")


def page_rel_paths(conversation_id: str, file_id: str, pages: int) -> list[str]:
    """Build the ordered list of page-PNG relative paths for a stored file.

    Deterministic: pages ``1..pages`` in order. The caller (rehydration) relies
    on this fixed ordering to keep the KV-cache prefix byte-identical turn to
    turn. Paths pointing at a missing file are skipped by the loader, not here.
    """
    conv = _sanitize_component(conversation_id)
    fid = _sanitize_component(file_id)
    return [f"{conv}/{fid}/{i}.png" for i in range(1, max(0, int(pages or 0)) + 1)]


def load_text(conversation_id: str, file_id: str) -> str | None:
    """Read back the page-labeled ``text.txt`` for a stored file, if present."""
    conv = _sanitize_component(conversation_id)
    fid = _sanitize_component(file_id)
    return _read_text_rel(f"{conv}/{fid}/text.txt")


def _read_text_rel(rel_path: str) -> str | None:
    p = _resolve(rel_path)
    if p is None or not p.exists() or not p.is_file():
        return None
    try:
        return p.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return None


def guess_mime(rel_path: str) -> str:
    """Best-effort mime type from a stored path's extension (for HTTP responses)."""
    ext = rel_path.rsplit(".", 1)[-1].lower() if "." in rel_path else "png"
    if ext == "jpg":
        ext = "jpeg"
    if ext == "svg":
        return "image/svg+xml"
    if ext == "pdf":
        return "application/pdf"
    if ext == "txt":
        return "text/plain; charset=utf-8"
    return f"image/{ext}"


# --------------------------------------------------------------------------- #
# Delete / prune (one folder per conversation)
# --------------------------------------------------------------------------- #

def delete_conversation_files(conversation_id: str) -> int:
    """Remove the on-disk folder for a single conversation. Returns the number
    of files deleted. Safe to call when the folder doesn't exist."""
    conv_dir = _conv_dir_path(conversation_id)
    try:
        conv_dir.resolve().relative_to(FILES_DIR.resolve())
    except ValueError:
        log.warning("file_store: refusing to delete dir outside FILES_DIR: %r", conversation_id)
        return 0
    if not conv_dir.exists():
        return 0
    count = sum(1 for _ in conv_dir.rglob("*") if _.is_file())
    shutil.rmtree(conv_dir, ignore_errors=True)
    log.info("file_store: deleted %d file(s) for conversation %s", count, conversation_id)
    return count


def delete_all_files() -> None:
    """Remove every stored chat file (used when clearing all conversations)."""
    if FILES_DIR.exists():
        shutil.rmtree(FILES_DIR, ignore_errors=True)
        log.info("file_store: cleared all chat files")


def prune_orphaned_files(valid_conversation_ids) -> int:
    """Delete folders whose conversation id is no longer in the DB (used after
    the 30-day conversation cleanup). Returns the number of folders removed."""
    if not FILES_DIR.exists():
        return 0
    valid = {_sanitize_component(cid) for cid in valid_conversation_ids}
    removed = 0
    for child in FILES_DIR.iterdir():
        if child.is_dir() and child.name not in valid:
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
    if removed:
        log.info("file_store: pruned %d orphaned file folder(s)", removed)
    return removed


# --------------------------------------------------------------------------- #
# Migration: old image-only layout → new per-file layout
# --------------------------------------------------------------------------- #

def migrate_legacy_image(conversation_id: str, legacy_rel_path: str) -> dict | None:
    """Move one legacy image (``chat_images/<conv>/<uuid>.<ext>``) into the new
    ``chat_files`` per-file layout and return the new lean file object.

    The legacy file becomes a one-page, no-text "image" file:
      * ``chat_files/<conv>/<file_id>/1.png``  (the rendered page == the image),
      * ``chat_files/<conv>/raw/<file_id>.<ext>`` (the original, a duplicate).

    Returns ``None`` (and leaves the legacy file untouched) if the source is
    missing or can't be read, so a bad row can't abort the whole migration.
    """
    import image_store  # legacy store, still present for the old chat_images dir

    raw = image_store.load_bytes(legacy_rel_path)
    if raw is None:
        return None

    # Preserve the original extension for raw/, default png.
    ext = legacy_rel_path.rsplit(".", 1)[-1].lower() if "." in legacy_rel_path else "png"
    # Derive a display name from the original filename.
    name = legacy_rel_path.rsplit("/", 1)[-1] if "/" in legacy_rel_path else legacy_rel_path

    file_id = new_file_id()
    write_original(conversation_id, file_id, raw, ext)
    # The rendered page must be a real PNG (reader + /chat-file serve pages as
    # <index>.png). If the legacy image wasn't PNG, transcode it so the bytes
    # match the .png name; fall back to the raw bytes if transcoding fails
    # (browsers content-sniff, so a mislabelled image still renders).
    page_bytes = raw
    if ext != "png":
        try:
            import pymupdf

            pix = pymupdf.Pixmap(io.BytesIO(raw))
            if pix.alpha:
                pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
            page_bytes = pix.tobytes("png")
        except Exception:  # noqa: BLE001
            page_bytes = raw
    write_page_png(conversation_id, file_id, 1, page_bytes)

    return {
        "type": "image",
        "path": file_id,
        "name": name or "image",
        "pages": 1,
        "hasText": False,
    }
