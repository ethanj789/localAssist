"""Image storage for chat messages.

Images attached to chat messages are stored as files on disk under
``chat_images/<conversation_id>/<uuid>.<ext>`` rather than inline in the
SQLite database. The ``messages.images`` column holds only a JSON list of
*relative* paths (relative to ``IMAGES_DIR``).

This separation matters:
  * The DB stays small — no multi-hundred-KB base64 blobs per row.
  * On history reload we serve the bytes from disk for display only.
  * The model only ever receives an image on the turn it was sent (the agent
    attaches fresh base64 for that turn), so stale images never re-enter the
    context window / KV cache on later turns.

Browsers/extensions produce data URLs shaped like
``data:image/png;base64,<b64>``. We strip the prefix, decode, and persist the
raw bytes. ``load_as_base64`` reverses this for feeding the model.
"""

from __future__ import annotations

import base64
import logging
import re
import uuid
from pathlib import Path

from config import ROOT_DIR

log = logging.getLogger(__name__)

IMAGES_DIR = ROOT_DIR / "chat_images"

# data:image/png;base64,AAAA...  →  capture mime subtype + payload
_DATA_URL_RE = re.compile(r"^data:image/(?P<ext>[a-zA-Z0-9.+-]+);base64,(?P<b64>.+)$", re.DOTALL)

# Map a few mime subtypes to sensible file extensions.
_EXT_MAP = {
    "jpeg": "jpg",
    "svg+xml": "svg",
}

# Guard against pathological payloads (~15 MB of base64 ≈ 11 MB decoded).
_MAX_B64_CHARS = 20_000_000


def _sanitize_conv_id(conversation_id: str) -> str:
    """Reduce a conversation id to the characters valid in our uuid-based ids,
    guarding against path traversal via a crafted id."""
    return re.sub(r"[^A-Za-z0-9_-]", "", conversation_id or "misc") or "misc"


def _conv_dir_path(conversation_id: str) -> Path:
    """Return the per-conversation image directory path (does NOT create it)."""
    return IMAGES_DIR / _sanitize_conv_id(conversation_id)


def _safe_conv_dir(conversation_id: str) -> Path:
    """Return (and create) the per-conversation image directory."""
    conv_dir = _conv_dir_path(conversation_id)
    conv_dir.mkdir(parents=True, exist_ok=True)
    return conv_dir


def delete_conversation_images(conversation_id: str) -> int:
    """Remove the on-disk image folder for a single conversation. Returns the
    number of files deleted. Safe to call when the folder doesn't exist."""
    import shutil

    conv_dir = _conv_dir_path(conversation_id)
    # Keep the delete scoped strictly inside IMAGES_DIR.
    try:
        conv_dir.resolve().relative_to(IMAGES_DIR.resolve())
    except ValueError:
        log.warning("image_store: refusing to delete dir outside IMAGES_DIR: %r", conversation_id)
        return 0
    if not conv_dir.exists():
        return 0
    count = sum(1 for _ in conv_dir.rglob("*") if _.is_file())
    shutil.rmtree(conv_dir, ignore_errors=True)
    log.info("image_store: deleted %d image(s) for conversation %s", count, conversation_id)
    return count


def delete_all_images() -> None:
    """Remove every stored chat image (used when clearing all conversations)."""
    import shutil

    if IMAGES_DIR.exists():
        shutil.rmtree(IMAGES_DIR, ignore_errors=True)
        log.info("image_store: cleared all chat images")


def prune_orphaned_images(valid_conversation_ids) -> int:
    """Delete image folders whose conversation id is no longer in the DB.

    Used after the 30-day conversation cleanup so images don't outlive their
    chats. Returns the number of orphaned folders removed."""
    import shutil

    if not IMAGES_DIR.exists():
        return 0
    valid = {_sanitize_conv_id(cid) for cid in valid_conversation_ids}
    removed = 0
    for child in IMAGES_DIR.iterdir():
        if child.is_dir() and child.name not in valid:
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
    if removed:
        log.info("image_store: pruned %d orphaned image folder(s)", removed)
    return removed


def parse_data_url(data_url: str) -> tuple[str, bytes] | None:
    """Split a ``data:image/...;base64,...`` URL into (extension, raw_bytes).

    Returns None if the string isn't a valid base64 image data URL.
    """
    if not isinstance(data_url, str):
        return None
    m = _DATA_URL_RE.match(data_url.strip())
    if not m:
        return None
    b64 = m.group("b64")
    if len(b64) > _MAX_B64_CHARS:
        log.warning("image_store: rejecting oversized data URL (%d b64 chars)", len(b64))
        return None
    ext = m.group("ext").lower()
    ext = _EXT_MAP.get(ext, ext)
    try:
        raw = base64.b64decode(b64, validate=False)
    except Exception as exc:
        log.warning("image_store: failed to decode data URL: %s", exc)
        return None
    return ext, raw


def save_data_url(conversation_id: str, data_url: str) -> str | None:
    """Persist a single data-URL image to disk.

    Returns the stored *relative* path (POSIX-style, relative to IMAGES_DIR),
    or None if the input could not be parsed.
    """
    parsed = parse_data_url(data_url)
    if parsed is None:
        return None
    ext, raw = parsed
    conv_dir = _safe_conv_dir(conversation_id)
    filename = f"{uuid.uuid4().hex}.{ext}"
    (conv_dir / filename).write_bytes(raw)
    safe_id = conv_dir.name
    rel = f"{safe_id}/{filename}"
    log.info("image_store: saved %s (%d bytes)", rel, len(raw))
    return rel


def save_data_urls(conversation_id: str, data_urls: list[str]) -> list[str]:
    """Persist a list of data-URL images. Returns the list of relative paths for
    the ones that saved successfully (skipping any that failed to parse)."""
    paths: list[str] = []
    for url in data_urls or []:
        rel = save_data_url(conversation_id, url)
        if rel:
            paths.append(rel)
    return paths


def _resolve(rel_path: str) -> Path | None:
    """Resolve a stored relative path to an absolute path inside IMAGES_DIR,
    refusing anything that escapes the directory."""
    if not rel_path or not isinstance(rel_path, str):
        return None
    candidate = (IMAGES_DIR / rel_path).resolve()
    try:
        candidate.relative_to(IMAGES_DIR.resolve())
    except ValueError:
        log.warning("image_store: refusing path outside IMAGES_DIR: %r", rel_path)
        return None
    return candidate


def load_bytes(rel_path: str) -> bytes | None:
    """Read raw image bytes for a stored relative path (for serving to the UI)."""
    p = _resolve(rel_path)
    if p is None or not p.exists():
        return None
    return p.read_bytes()


def load_as_base64(rel_path: str) -> str | None:
    """Read a stored image back as a bare base64 string (no data-URL prefix),
    which is the exact shape Ollama's /api/chat expects in ``images: [...]``."""
    raw = load_bytes(rel_path)
    if raw is None:
        return None
    return base64.b64encode(raw).decode("utf-8")


def guess_mime(rel_path: str) -> str:
    """Best-effort mime type from a stored path's extension, for HTTP responses."""
    ext = rel_path.rsplit(".", 1)[-1].lower() if "." in rel_path else "png"
    if ext == "jpg":
        ext = "jpeg"
    if ext == "svg":
        return "image/svg+xml"
    return f"image/{ext}"


def data_url_from_path(rel_path: str) -> str | None:
    """Rebuild a browser-renderable data URL from a stored path (for restore)."""
    b64 = load_as_base64(rel_path)
    if b64 is None:
        return None
    return f"data:{guess_mime(rel_path)};base64,{b64}"
