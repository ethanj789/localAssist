"""
tool_apps/notes/ocr/ — RETIRED image-OCR pipeline (TrOCR / Moondream).

This package holds the legacy handwriting OCR pipeline that rendered stroke
blobs to images and ran them through an ML model (TrOCR by default, Moondream
via Ollama as an alternative). It has been superseded by native Windows Ink
recognition (see winink/ and tool_apps/notes/winink_recognizer.py).

Kept intact for reference and easy rollback. Nothing in the live app imports
from here anymore.
"""
