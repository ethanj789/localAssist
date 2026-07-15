"""
tool_apps/notes/_trocr_backend.py — TrOCR handwriting backend.

Uses microsoft/trocr-base-handwritten (~350 MB, downloaded once to HF cache).
TrOCR is an encoder-decoder trained specifically on handwritten text —
no prompts, no narration, just raw transcription.

Row-slicing strategy:
    Each blob tile is sliced into horizontal strips before inference.
    TrOCR was trained on line images (~32px tall), so giving it a full
    tall tile hurts accuracy. We find natural row breaks by projecting
    strokes onto the Y axis and cutting at low-ink valleys.

    This logic lives entirely inside this backend — process_page and the
    OCRBackend protocol are untouched.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
from PIL import Image

log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────

TROCR_MODEL: str = "microsoft/trocr-base-handwritten"

# Minimum row height (pixels in the scaled tile) worth sending to the model.
# Strips shorter than this are likely stray marks, not text lines.
MIN_ROW_HEIGHT: int = 20

# How much vertical padding to add above/below each row crop before inference.
ROW_PADDING: int = 8


# ── Column splitter ────────────────────────────────────────────────────────────

# Tiles wider than this aspect ratio get split into columns before row-slicing.
# A 2:1 ratio means width > 2x height — typical for two-column note layouts.
COLUMN_SPLIT_ASPECT: float = 1.8
MIN_COLUMN_WIDTH: int = 100  # px — don't split narrower than this per column


def _split_into_columns(img: Image.Image) -> list[Image.Image]:
    """
    Split a wide tile into vertical column strips using a vertical ink
    projection profile, then return strips left-to-right.

    Falls back to returning the whole image if no clear column gap is found
    or the tile isn't wide enough to warrant splitting.
    """
    w, h = img.size
    if w < h * COLUMN_SPLIT_ASPECT:
        return [img]  # not wide enough to be multi-column

    grey = np.array(img.convert("L"))
    ink  = 255 - grey
    col_sums = ink.sum(axis=0).astype(float)  # sum along rows → per-column profile

    threshold = col_sums.max() * 0.01
    is_empty  = col_sums <= threshold

    # Find the widest contiguous empty gap in the middle third of the tile
    mid_lo = w // 3
    mid_hi = 2 * w // 3
    best_gap_start = best_gap_end = best_gap_len = 0

    gap_start = None
    for x in range(mid_lo, mid_hi):
        if is_empty[x]:
            if gap_start is None:
                gap_start = x
        else:
            if gap_start is not None:
                gap_len = x - gap_start
                if gap_len > best_gap_len:
                    best_gap_len, best_gap_start, best_gap_end = gap_len, gap_start, x
                gap_start = None
    if gap_start is not None:
        gap_len = mid_hi - gap_start
        if gap_len > best_gap_len:
            best_gap_len, best_gap_start, best_gap_end = gap_len, gap_start, mid_hi

    if best_gap_len < 4:
        return [img]  # no clear gap found

    split_x = (best_gap_start + best_gap_end) // 2
    left  = img.crop((0,       0, split_x, h))
    right = img.crop((split_x, 0, w,       h))

    if left.width < MIN_COLUMN_WIDTH or right.width < MIN_COLUMN_WIDTH:
        return [img]

    return [left, right]


# ── Row splitter ───────────────────────────────────────────────────────────────

def _split_into_rows(img: Image.Image) -> list[Image.Image]:
    """
    Slice a tile image into horizontal row crops using a horizontal ink
    projection profile.

    Algorithm:
      1. Convert to greyscale, invert (ink = high value).
      2. Sum each row → 1-D profile.
      3. Find valleys (near-zero rows) — these are the gaps between lines.
      4. Yield crops between consecutive valleys, with ROW_PADDING added.

    Falls back to returning the whole image as one crop if no clear rows
    are found (e.g. a single short word).
    """
    grey = np.array(img.convert("L"))
    # Invert: white background → 0, dark ink → positive
    ink = 255 - grey
    row_sums = ink.sum(axis=1).astype(float)

    # Threshold: a row is "empty" if its ink sum is below 1% of the max
    threshold = row_sums.max() * 0.01
    is_empty = row_sums <= threshold

    h, w = grey.shape
    in_text = False
    row_starts: list[int] = []
    row_ends: list[int] = []

    for y, empty in enumerate(is_empty):
        if not in_text and not empty:
            in_text = True
            row_starts.append(y)
        elif in_text and empty:
            in_text = False
            row_ends.append(y)
    if in_text:
        row_ends.append(h)

    if not row_starts:
        return [img]  # no ink found at all

    crops: list[Image.Image] = []
    for y0, y1 in zip(row_starts, row_ends):
        if y1 - y0 < MIN_ROW_HEIGHT:
            continue
        top = max(0, y0 - ROW_PADDING)
        bot = min(h, y1 + ROW_PADDING)
        crops.append(img.crop((0, top, w, bot)))

    return crops if crops else [img]


# ── TrOCR backend ──────────────────────────────────────────────────────────────

class TrOCRBackend:
    """
    Handwriting OCR backend using microsoft/trocr-base-handwritten.

    load() downloads and caches the model on first call (~350 MB).
    recognize() slices the tile into rows, runs TrOCR on each, and
    returns one region dict per non-empty row with an approximate bbox.
    """

    def __init__(self) -> None:
        self._processor = None
        self._model = None

    def load(self) -> None:
        if self._model is not None:
            return
        log.info("TrOCRBackend: loading %s (first run downloads ~350 MB)…", TROCR_MODEL)
        from transformers import TrOCRProcessor, VisionEncoderDecoderModel
        self._processor = TrOCRProcessor.from_pretrained(TROCR_MODEL)
        self._model = VisionEncoderDecoderModel.from_pretrained(TROCR_MODEL)
        self._model.eval()
        log.info("TrOCRBackend: ready.")

    def recognize(self, image: Image.Image) -> list[dict]:
        if self._model is None:
            self.load()

        try:
            import torch
            from PIL import ImageOps

            # Auto-invert dark images: TrOCR expects black text on white.
            # If the tile is mostly dark (mean < 128), invert it.
            grey_mean = np.array(image.convert("L")).mean()
            if grey_mean < 128:
                image = ImageOps.invert(image.convert("RGB"))

            columns = _split_into_columns(image)
            results: list[dict] = []
            col_x_offset = 0

            for col_img in columns:
                rows = _split_into_rows(col_img)
                y_offset = 0

                for crop in rows:
                    rgb = crop.convert("RGB")
                    pixel_values = self._processor(
                        images=rgb, return_tensors="pt"
                    ).pixel_values

                    with torch.no_grad():
                        generated_ids = self._model.generate(
                            pixel_values,
                            max_new_tokens=128,
                        )

                    text = self._processor.batch_decode(
                        generated_ids, skip_special_tokens=True
                    )[0].strip()

                    if text:
                        results.append({
                            "text": text,
                            "bbox": {
                                "minX": float(col_x_offset),
                                "minY": float(y_offset),
                                "maxX": float(col_x_offset + col_img.width),
                                "maxY": float(y_offset + crop.height),
                            },
                        })

                    y_offset += crop.height

                col_x_offset += col_img.width

            # Sort top-to-bottom, left-to-right
            results.sort(key=lambda r: (r["bbox"]["minY"], r["bbox"]["minX"]))
            return results

        except Exception as exc:
            log.error("TrOCRBackend.recognize: %s", exc)
            return []
