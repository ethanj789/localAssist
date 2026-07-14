"""
tool_apps/notes/ocr_pipeline.py — handwritten note OCR pipeline.

Flow per page:
  1. cluster_strokes_into_blobs  — connected-component grouping (gap=200px)
  2. hash_strokes                — sha1 content fingerprint
  3. render_tile                 — PIL image of that blob
  4. backend.recognize()         — TrOCR (or any OCRBackend)
  5. write plain .txt (no header) + .hash sidecar to:
         aiWorkspace/notesAppText/<title-slug>/<blob_idx>.txt

Output directory is derived from the page's title in meta.json so folders
are human-readable (e.g. "math-notes/") rather than date-slugs.
Incremental skip uses the .hash sidecar instead of an in-file header.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

log = logging.getLogger(__name__)

# ── Output root ────────────────────────────────────────────────────────────────
OUTPUT_ROOT = (
    Path(__file__).resolve().parent.parent.parent / "aiWorkspace" / "notesAppText"
)

# Notes data root — used to resolve meta.json for title lookup
_NOTES_DATA_ROOT = Path(__file__).resolve().parent / "data"


# ── Title-slug resolution ──────────────────────────────────────────────────────

def _slugify(value: str) -> str:
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "untitled"


def _page_output_dir(page_id: str, output_root: Path) -> Path:
    """
    Return the output directory for a page's OCR text files.

    Tries to read the page title from meta.json and slugify it.
    Falls back to page_id if meta.json is missing or has no title.

    Examples:
        "Math Notes"     → notesAppText/math-notes/
        "Untitled"       → notesAppText/untitled/
        (no meta.json)   → notesAppText/2026-07-13_untitled/
    """
    for type_dir in ("notes", "art"):
        meta_path = _NOTES_DATA_ROOT / type_dir / page_id / "meta.json"
        if meta_path.exists():
            try:
                with meta_path.open("r", encoding="utf-8") as f:
                    meta = json.load(f)
                title = (meta.get("title") or "").strip()
                if title and title.lower() != "untitled":
                    return output_root / _slugify(title)
            except Exception:
                pass
            break
    # Fallback: use slug of page_id
    return output_root / _slugify(page_id)


# ── Blob clustering ────────────────────────────────────────────────────────────

def _stroke_bbox(stroke: dict) -> dict | None:
    """Return {minX, minY, maxX, maxY} for a single stroke or None if no points."""
    points = stroke.get("points", [])
    if not points:
        return None
    xs = [p.get("x", p[0] if isinstance(p, (list, tuple)) else 0) for p in points]
    ys = [p.get("y", p[1] if isinstance(p, (list, tuple)) else 0) for p in points]
    return {"minX": min(xs), "minY": min(ys), "maxX": max(xs), "maxY": max(ys)}


def _bboxes_overlap(a: dict, b: dict, gap: float) -> bool:
    return (
        a["minX"] - gap <= b["maxX"]
        and b["minX"] - gap <= a["maxX"]
        and a["minY"] - gap <= b["maxY"]
        and b["minY"] - gap <= a["maxY"]
    )


def _merge_bboxes(bboxes: list[dict]) -> dict:
    return {
        "minX": min(b["minX"] for b in bboxes),
        "minY": min(b["minY"] for b in bboxes),
        "maxX": max(b["maxX"] for b in bboxes),
        "maxY": max(b["maxY"] for b in bboxes),
    }


def cluster_strokes_into_blobs(
    strokes: list[dict], gap_threshold: float = 200.0
) -> list[dict]:
    """
    Group strokes into spatially-connected blobs.
    Returns [{blob_idx, strokes, bbox}] sorted top-to-bottom.
    """
    items: list[tuple[dict, dict]] = []
    for s in strokes:
        bb = _stroke_bbox(s)
        if bb is not None:
            items.append((bb, s))

    if not items:
        return []

    parent = list(range(len(items)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        parent[find(x)] = find(y)

    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if _bboxes_overlap(items[i][0], items[j][0], gap_threshold):
                union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(len(items)):
        groups.setdefault(find(i), []).append(i)

    blobs: list[dict] = []
    for member_idxs in groups.values():
        member_strokes = [items[i][1] for i in member_idxs]
        member_bboxes  = [items[i][0] for i in member_idxs]
        blobs.append({
            "strokes": member_strokes,
            "bbox": _merge_bboxes(member_bboxes),
        })

    blobs.sort(key=lambda b: b["bbox"]["minY"])
    for idx, blob in enumerate(blobs):
        blob["blob_idx"] = idx

    return blobs


# ── Stroke hashing ─────────────────────────────────────────────────────────────

def hash_strokes(strokes: list[dict]) -> str:
    h = hashlib.sha1()
    for s in sorted(strokes, key=lambda x: str(x.get("id", ""))):
        h.update(str(s.get("id", "")).encode())
        h.update(repr(s.get("points", [])).encode())
    return h.hexdigest()


# ── Tile rendering ─────────────────────────────────────────────────────────────

def render_tile(
    strokes: list[dict],
    bbox: dict,
    padding: int = 16,
    scale: float = 2.0,
) -> Image.Image:
    w = int((bbox["maxX"] - bbox["minX"] + 2 * padding) * scale)
    h = int((bbox["maxY"] - bbox["minY"] + 2 * padding) * scale)
    w, h = max(w, 1), max(h, 1)

    img  = Image.new("RGB", (w, h), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    ox   = bbox["minX"] - padding
    oy   = bbox["minY"] - padding

    for stroke in strokes:
        points = stroke.get("points", [])
        if not points:
            continue

        def _pt(p: Any) -> tuple[float, float, float]:
            if isinstance(p, dict):
                return p.get("x", 0.0), p.get("y", 0.0), p.get("pressure", 0.5)
            if isinstance(p, (list, tuple)) and len(p) >= 2:
                return float(p[0]), float(p[1]), float(p[2]) if len(p) > 2 else 0.5
            return 0.0, 0.0, 0.5

        pts = [_pt(p) for p in points]
        if len(pts) == 1:
            pts = [pts[0], (pts[0][0] + 1.0, pts[0][1], pts[0][2])]

        for i in range(len(pts) - 1):
            x0, y0, p0 = pts[i]
            x1, y1, p1 = pts[i + 1]
            sx0 = (x0 - ox) * scale
            sy0 = (y0 - oy) * scale
            sx1 = (x1 - ox) * scale
            sy1 = (y1 - oy) * scale
            line_w = max(1, int(((p0 + p1) / 2.0) * 4 * scale))
            draw.line([(sx0, sy0), (sx1, sy1)], fill=(0, 0, 0), width=line_w)

    return img


# ── Hash sidecar I/O ───────────────────────────────────────────────────────────

def _read_stored_hash(txt_path: Path) -> str | None:
    """Read the content hash from the .hash sidecar next to a .txt file."""
    hash_path = txt_path.with_suffix(".hash")
    try:
        return hash_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _write_blob_files(txt_path: Path, text: str, content_hash: str) -> None:
    """Write plain .txt (no header) and a .hash sidecar."""
    txt_path.write_text(text, encoding="utf-8")
    txt_path.with_suffix(".hash").write_text(content_hash, encoding="utf-8")


# ── Main entry point ───────────────────────────────────────────────────────────

def process_page(
    page_id: str,
    strokes: list[dict],
    output_dir: Path | None = None,
) -> None:
    """
    Run the full OCR pipeline for one page.

    Writes to notesAppText/<title-slug>/<blob_idx>.txt (plain text, no header).
    Incremental: blobs whose hash_strokes matches the .hash sidecar are skipped.
    """
    from tool_apps.notes.ocr_backends import get_backend

    if output_dir is None:
        output_dir = OUTPUT_ROOT

    page_out = _page_output_dir(page_id, output_dir)
    page_out.mkdir(parents=True, exist_ok=True)

    # Write a .meta sidecar in the output dir so scan_note_blobs can
    # recover the page_id from the title-slug directory name.
    meta_sidecar = page_out / ".page_meta.json"
    if not meta_sidecar.exists():
        try:
            meta_sidecar.write_text(
                json.dumps({"page_id": page_id}), encoding="utf-8"
            )
        except Exception:
            pass

    blobs = cluster_strokes_into_blobs(strokes)
    if not blobs:
        log.info("process_page[%s]: no strokes / blobs found, skipping.", page_id)
        return

    backend = get_backend()
    processed = skipped = 0

    for blob in blobs:
        blob_idx    = blob["blob_idx"]
        blob_strokes = blob["strokes"]
        bbox        = blob["bbox"]

        content_hash = hash_strokes(blob_strokes)
        out_path     = page_out / f"{blob_idx}.txt"

        # Incremental: skip if hash sidecar matches
        if _read_stored_hash(out_path) == content_hash:
            skipped += 1
            continue

        tile    = render_tile(blob_strokes, bbox)
        regions = backend.recognize(tile)
        regions.sort(key=lambda r: r.get("bbox", {}).get("minY", 0))
        text_body = "\n".join(r["text"] for r in regions if r.get("text"))

        _write_blob_files(out_path, text_body, content_hash)
        processed += 1
        log.info(
            "process_page[%s]: blob %d → %d region(s) → %s",
            page_id, blob_idx, len(regions), page_out.name,
        )

    log.info(
        "process_page[%s]: done — %d processed, %d skipped.",
        page_id, processed, skipped,
    )
