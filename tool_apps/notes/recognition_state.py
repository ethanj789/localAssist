"""
tool_apps/notes/recognition_state.py — notes-app-private recognition bookkeeping.

Keeps all per-page recognition metadata OUT of the agent-visible
`aiWorkspace/notesAppText/` tree, so that tree contains nothing but the plain
recognized text (`<slug>/0.txt`) the assistant searches and reads.

For each page type ("notes" / "art") we persist a single JSON map at:

    tool_apps/notes/data/<type>/_recognition.json
        { "<page_id>": {"slug": "<title-slug>", "hash": "<strokes-sha1>"}, ... }

  - "hash" is the SHA1 of the page's source strokes at the time its text was
    last recognized. It is the recognizer's skip-gate: if the current strokes
    hash matches, recognition (an expensive Windows Ink subprocess) is skipped.
  - "slug" records which `notesAppText/<slug>/` directory holds this page's
    text. It is the reverse map delete/rename use to find and manage that dir
    without scanning sidecars.

This module is the single source of truth for both; it owns no business logic
beyond load/save/lookup and is safe to import from both api.py and
winink_recognizer.py (no circular imports).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# tool_apps/notes/recognition_state.py -> tool_apps/notes/data
_DATA_ROOT = Path(__file__).resolve().parent / "data"

_MAP_FILENAME = "_recognition.json"


def _map_path(page_type: str) -> Path:
    return _DATA_ROOT / page_type / _MAP_FILENAME


def load_map(page_type: str) -> dict[str, dict]:
    """Return {page_id: {"slug": str, "hash": str}} for a type. Missing/corrupt → {}."""
    path = _map_path(page_type)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("recognition_state: failed to read %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    # Normalise: tolerate legacy/partial shapes.
    out: dict[str, dict] = {}
    for page_id, entry in data.items():
        if isinstance(entry, dict):
            out[page_id] = {
                "slug": entry.get("slug") or "",
                "hash": entry.get("hash") or "",
            }
    return out


def save_map(page_type: str, state: dict[str, dict]) -> None:
    path = _map_path(page_type)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)


def get_entry(page_type: str, page_id: str) -> Optional[dict]:
    return load_map(page_type).get(page_id)


def get_hash(page_type: str, page_id: str) -> Optional[str]:
    entry = get_entry(page_type, page_id)
    return entry.get("hash") if entry else None


def get_slug(page_type: str, page_id: str) -> Optional[str]:
    entry = get_entry(page_type, page_id)
    slug = entry.get("slug") if entry else None
    return slug or None


def set_entry(page_type: str, page_id: str, slug: str, content_hash: str) -> None:
    state = load_map(page_type)
    state[page_id] = {"slug": slug, "hash": content_hash}
    save_map(page_type, state)


def update_slug(page_type: str, page_id: str, slug: str) -> None:
    """Point a page at a new text-dir slug (used on rename). No-op if unknown."""
    state = load_map(page_type)
    entry = state.get(page_id)
    if entry is None:
        return
    entry["slug"] = slug
    save_map(page_type, state)


def remove_entry(page_type: str, page_id: str) -> Optional[str]:
    """Drop a page's entry (used on delete). Returns its last-known slug, if any."""
    state = load_map(page_type)
    entry = state.pop(page_id, None)
    if entry is None:
        return None
    save_map(page_type, state)
    return entry.get("slug") or None


def slug_owner(page_type: str, slug: str) -> Optional[str]:
    """Return the page_id that currently owns a given text-dir slug, if any."""
    for page_id, entry in load_map(page_type).items():
        if entry.get("slug") == slug:
            return page_id
    return None
