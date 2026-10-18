"""Single entry point for turning an uploaded file into model-ready artifacts.

``prep_file(conversation_id, name, data_url)`` is the one call the agent makes
per attachment. It:

  1. decodes the ``data:<mime>;base64,...`` URL,
  2. dispatches on mime type to a registered extractor (``EXTRACTORS``),
  3. renders page images + pulls per-page text from the file's own text layer
     (NO OCR — rendered pages are a parallel output, never the text source),
  4. writes the original bytes, the rendered page PNGs, and a page-labeled
     ``text.txt`` to disk via ``file_store``,
  5. returns a lean DB ``file object`` plus the page-labeled text string to
     splice into the upload turn's message content.

Every extractor returns the same universal shape so adding a new file type
later is "write one extractor, nothing else"::

    { "kind": "<type>", "pages": [ { "index": 1, "image_bytes": b"...", "text": "..." }, ... ] }

Priority is Ollama-first (see ``.kiro/steering/project-focus.md``). Page images
ride Ollama's per-message ``images: [bare b64]`` channel; extracted text is
spliced into the message ``content``.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass

import file_store

log = logging.getLogger(__name__)

# Cap the longest edge of a rendered PDF page (in pixels). A full-page 2x render
# (~1224x1584) crashes the Ollama vision runner; ~1024px keeps the image in the
# same size range as the screenshots that already work, while staying legible.
MAX_PAGE_PX = 1024
# Never upscale: a small page shouldn't be blown up past its natural size.
# Caps the scale factor at 2x the 72-DPI base so tiny pages still read well.
MAX_PAGE_SCALE = 2.0


@dataclass
class PreparedFile:
    """Result of preparing one uploaded file."""

    db_object: dict          # lean object stored in messages.files (see db.py)
    text: str                # page-labeled text to splice into message content
    page_rel_paths: list[str]  # ordered page-PNG relative paths (for this turn)


# --------------------------------------------------------------------------- #
# Extractors — each returns {kind, pages: [{index, image_bytes, text}]}
# --------------------------------------------------------------------------- #

def _extract_pdf(raw: bytes) -> dict:
    """Render PDF pages to PNG and pull per-page text in a single pass (PyMuPDF).

    Caps at ``file_store.MAX_PAGES``; the caller adds a truncation note to the
    model when the source had more pages than were rendered.
    """
    import pymupdf  # PyMuPDF (aka fitz)

    pages: list[dict] = []
    total_pages = 0
    with pymupdf.open(stream=raw, filetype="pdf") as doc:
        total_pages = doc.page_count
        for i, page in enumerate(doc):
            if i >= file_store.MAX_PAGES:
                break
            # Scale the render so the page's longest edge lands at ~MAX_PAGE_PX,
            # never upscaling. A full-page 2x render (1224x1584) is large enough
            # to crash the Ollama vision runner; capping to the size range of a
            # normal screenshot keeps the image token count in the range that
            # already works, while staying legible. page.rect is in 72-DPI points.
            rect = page.rect
            longest = max(rect.width, rect.height) or 1.0
            scale = min(MAX_PAGE_PX / longest, MAX_PAGE_SCALE)
            matrix = pymupdf.Matrix(scale, scale)
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            png_bytes = pix.tobytes("png")
            text = page.get_text("text") or ""
            pages.append({"index": i + 1, "image_bytes": png_bytes, "text": text.strip()})
    return {"kind": "pdf", "pages": pages, "total_pages": total_pages}


def _extract_image(raw: bytes) -> dict:
    """Trivial pass-through: an image is a one-page file with no text layer."""
    return {
        "kind": "image",
        "pages": [{"index": 1, "image_bytes": raw, "text": ""}],
        "total_pages": 1,
    }


# Registry keyed by a normalized file "type". The dispatcher maps a mime type
# to one of these keys.
EXTRACTORS = {
    "pdf": _extract_pdf,
    "image": _extract_image,
}


def _type_from_mime(mime: str) -> str | None:
    """Map a mime type to an extractor key, or None if unsupported."""
    mime = (mime or "").lower()
    if mime == "application/pdf":
        return "pdf"
    if mime.startswith("image/"):
        return "image"
    return None


def _original_ext(mime: str, file_type: str) -> str:
    """Pick a sensible extension for the stored original bytes."""
    mime = (mime or "").lower()
    if file_type == "pdf":
        return "pdf"
    # image/<subtype>
    if "/" in mime:
        sub = mime.split("/", 1)[1]
        sub = {"jpeg": "jpg", "svg+xml": "svg"}.get(sub, sub)
        # Strip any parameters just in case (e.g. "png; charset=...").
        return sub.split(";", 1)[0].strip() or "bin"
    return "bin"


# --------------------------------------------------------------------------- #
# Text formatting (page-labeled, written once at upload)
# --------------------------------------------------------------------------- #

def _format_text(name: str, rendered_pages: list[dict], total_pages: int, has_text: bool) -> str:
    """Build the page-labeled text block spliced into the upload message and
    written to ``text.txt``. Deterministic given the same inputs so history
    rebuilds stay byte-identical."""
    rendered = len(rendered_pages)
    header_count = total_pages if total_pages else rendered
    header = (
        f"[Attached: {name} — {header_count} page{'s' if header_count != 1 else ''}. "
        f"Page images follow in order.]"
    )
    if total_pages > rendered:
        header += (
            f"\n[Note: only the first {rendered} of {total_pages} pages were "
            f"rendered/extracted; the rest were truncated.]"
        )

    if not has_text:
        return header

    parts = [header, ""]
    for page in rendered_pages:
        idx = page["index"]
        text = (page.get("text") or "").strip()
        if text:
            parts.append(f"--- {name}, page {idx} ---")
            parts.append(text)
        else:
            parts.append(f"--- {name}, page {idx} (no extractable text) ---")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# The one entry point
# --------------------------------------------------------------------------- #

def prep_file(conversation_id: str, name: str, data_url: str) -> PreparedFile | None:
    """Prepare a single uploaded file: render pages + extract text once, persist
    to disk, and return a lean DB object plus the text to splice into the
    message content.

    Returns ``None`` if the data URL can't be parsed or the type is unsupported.
    """
    parsed = file_store.parse_data_url(data_url)
    if parsed is None:
        log.warning("prep_file: unparseable data URL for %r", name)
        return None
    mime, raw = parsed

    file_type = _type_from_mime(mime)
    if file_type is None or file_type not in EXTRACTORS:
        log.warning("prep_file: unsupported type %r for %r", mime, name)
        return None

    try:
        extracted = EXTRACTORS[file_type](raw)
    except Exception as exc:  # noqa: BLE001
        log.warning("prep_file: extractor failed for %r (%s): %s", name, mime, exc)
        return None

    rendered_pages = extracted["pages"]
    if not rendered_pages:
        log.warning("prep_file: no pages produced for %r", name)
        return None
    total_pages = extracted.get("total_pages", len(rendered_pages))

    file_id = file_store.new_file_id()
    safe_name = (name or f"{file_type}").strip() or file_type

    # 1) original bytes (kept so the chip can link to a download)
    original_ext = _original_ext(mime, file_type)
    file_store.write_original(conversation_id, file_id, raw, original_ext)

    # 2) rendered page PNGs (1-indexed, page order)
    page_rel_paths: list[str] = []
    for page in rendered_pages:
        rel = file_store.write_page_png(
            conversation_id, file_id, page["index"], page["image_bytes"]
        )
        page_rel_paths.append(rel)

    # 3) page-labeled text sidecar + the spliced text string
    has_text = any((p.get("text") or "").strip() for p in rendered_pages)
    text_block = _format_text(safe_name, rendered_pages, total_pages, has_text)
    if has_text:
        file_store.write_text(conversation_id, file_id, text_block)

    db_object = {
        "type": file_type,
        # Path is the file's own folder id (relative to the conversation); the
        # reader rebuilds page paths from pages count deterministically.
        "path": file_id,
        "name": safe_name,
        "pages": len(rendered_pages),
        "hasText": bool(has_text),
        # Extension of the stored original (raw/<file_id>.<ext>), so the UI can
        # build a stable download link to the real file instead of inlining a
        # giant base64 data URL.
        "ext": original_ext,
    }

    log.info(
        "prep_file: prepared %s (%s) — %d page(s), hasText=%s, file_id=%s",
        safe_name, file_type, len(rendered_pages), has_text, file_id,
    )
    return PreparedFile(db_object=db_object, text=text_block, page_rel_paths=page_rel_paths)
