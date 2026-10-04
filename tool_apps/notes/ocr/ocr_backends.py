"""
tool_apps/notes/ocr_backends.py — OCR backend protocol + Moondream implementation.

OCRBackend Protocol defines the interface every backend must satisfy.
MoondreamBackend is the active implementation: Ollama-served moondream,
one HTTP call per blob tile.

Config constants (one-line swap points):
    OCR_BACKEND  — selects the backend class in get_backend()
    OCR_MODEL    — Ollama model tag
"""

from __future__ import annotations

import logging
import re
from typing import Protocol, runtime_checkable

import requests
import base64
import io

from PIL import Image

log = logging.getLogger(__name__)

# ── Config constants ───────────────────────────────────────────────────────────

OCR_BACKEND: str = "trocr"
OCR_MODEL: str = "moondream"

def _ocr_prompt() -> str:
    try:
        from prompts import OCR_PROMPT
        return OCR_PROMPT
    except Exception:
        return "Extract all text exactly as written. Output plain text only. Do not add commentary."

def _ollama_base_url() -> str:
    try:
        from config import CONFIG
        return CONFIG.get("ollama_base_url", "http://localhost:11434")
    except Exception:
        return "http://localhost:11434"


# ── Protocol ──────────────────────────────────────────────────────────────────

@runtime_checkable
class OCRBackend(Protocol):
    def load(self) -> None:
        """Warm-up. Called once before the first recognize()."""
        ...

    def recognize(self, image: Image.Image) -> list[dict]:
        """
        Run OCR on a PIL image of one stroke blob.

        Returns:
            [{"text": str, "bbox": {"minX", "minY", "maxX", "maxY"}}, ...]
        Returns [] on failure — never raises.
        """
        ...


# ── Narration stripper ─────────────────────────────────────────────────────────

# Patterns moondream uses when it falls back to captioning despite the prompt.
_NARRATION_PREFIXES = re.compile(
    r'^(the image (shows|features|depicts|contains|has)|'
    r'this image (shows|features|depicts|contains)|'
    r'in this image|'
    r'i can see|'
    r'the (text|writing|word|sign|note|handwriting) (reads|says|appears|is|in the image))',
    re.IGNORECASE,
)

# Quoted text extractor — captures content inside any quote style
_QUOTED = re.compile(r'["\u201c\u2018\'](.*?)["\u201d\u2019\']', re.DOTALL)


def _clean_response(raw: str) -> str:
    """
    Best-effort extraction of actual text content from a moondream response.

    Strategy:
    1. If the response doesn't look like narration, return it as-is.
    2. If it does, try to extract quoted strings (the model usually quotes
       the actual text it read).
    3. If no quotes, strip the narration sentence(s) and return what's left.
    4. If nothing survives, return the original trimmed response so we never
       lose content silently.
    """
    text = raw.strip()
    if not text:
        return text

    # Check first line for narration pattern
    first_line = text.split('\n')[0]
    if not _NARRATION_PREFIXES.match(first_line):
        return text  # looks clean

    # Try to pull quoted content first
    quoted = _QUOTED.findall(text)
    if quoted:
        return "\n".join(q.strip() for q in quoted if q.strip())

    # Strip narration sentences (ends at first period/exclamation/question)
    # and return remainder
    stripped = re.sub(r'^[^.!?]*[.!?]\s*', '', text, count=1).strip()
    if stripped:
        return stripped

    # Last resort: return original
    return text


# ── Moondream backend ──────────────────────────────────────────────────────────

class MoondreamBackend:
    """
    Transcription backend using Moondream served through Ollama.
    Returns a single region whose bbox spans the full tile.
    """

    def __init__(self) -> None:
        self._base_url: str = ""

    def load(self) -> None:
        self._base_url = _ollama_base_url()
        log.info("MoondreamBackend: ready (model=%s, url=%s)", OCR_MODEL, self._base_url)

    def recognize(self, image: Image.Image) -> list[dict]:
        if not self._base_url:
            self.load()

        buf = io.BytesIO()
        image.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

        payload = {
            "model": OCR_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": _ocr_prompt(),
                    "images": [b64],
                }
            ],
            "stream": False,
        }

        try:
            resp = requests.post(
                f"{self._base_url}/api/chat",
                json=payload,
                timeout=120,
            )
            resp.raise_for_status()
            raw = resp.json().get("message", {}).get("content", "").strip()

            text = _clean_response(raw)

            if not text:
                log.debug("MoondreamBackend: empty response after cleaning (raw=%r)", raw[:80])
                return []

            log.debug("MoondreamBackend: raw=%r  →  cleaned=%r", raw[:80], text[:80])

            return [{
                "text": text,
                "bbox": {
                    "minX": 0.0,
                    "minY": 0.0,
                    "maxX": float(image.width),
                    "maxY": float(image.height),
                },
            }]

        except requests.exceptions.ConnectionError:
            log.error(
                "MoondreamBackend: could not connect to Ollama at %s — is it running?",
                self._base_url,
            )
            return []
        except Exception as exc:
            log.error("MoondreamBackend.recognize: %s", exc)
            return []


# ── Backend factory ────────────────────────────────────────────────────────────

_backend_instance: OCRBackend | None = None


def get_backend() -> OCRBackend:
    global _backend_instance
    if _backend_instance is not None:
        return _backend_instance

    if OCR_BACKEND == "moondream":
        backend: OCRBackend = MoondreamBackend()
    elif OCR_BACKEND == "easyocr":
        from tool_apps.notes.ocr._easyocr_backend import EasyOCRBackend
        backend = EasyOCRBackend()
    elif OCR_BACKEND == "trocr":
        from tool_apps.notes.ocr._trocr_backend import TrOCRBackend
        backend = TrOCRBackend()
    else:
        raise ValueError(f"Unknown OCR_BACKEND: {OCR_BACKEND!r}")

    backend.load()
    _backend_instance = backend
    return backend
