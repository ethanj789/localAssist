"""
Skill loader — reads skill JSON definitions from the skills/ folder.
"""

import json
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

SKILLS_DIR = Path(__file__).parent / "definitions"

# Cache of loaded skills (refreshed on each call for hot-reload during dev)
_skills_cache: list[dict] | None = None
_cache_mtime: float = 0.0


def load_skills(force_reload: bool = False) -> list[dict]:
    """Load all skill definitions from JSON files in the skills/ folder."""
    global _skills_cache, _cache_mtime

    # Simple mtime-based invalidation: reload if any json file is newer
    json_files = list(SKILLS_DIR.glob("*.json"))
    if not json_files:
        _skills_cache = []
        return []

    latest_mtime = max(f.stat().st_mtime for f in json_files)
    if not force_reload and _skills_cache is not None and latest_mtime <= _cache_mtime:
        return _skills_cache

    skills = []
    for file in json_files:
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
            # Validate required fields
            if not all(k in data for k in ("name", "command", "steps")):
                log.warning("Skipping invalid skill file %s: missing required fields", file.name)
                continue
            data["_source_file"] = file.name
            skills.append(data)
        except Exception as e:
            log.error("Failed to load skill %s: %s", file.name, e)

    _skills_cache = skills
    _cache_mtime = latest_mtime
    log.info("Loaded %d skills from %s", len(skills), SKILLS_DIR)
    return skills


def get_skill_by_command(command: str) -> Optional[dict]:
    """Find a skill by its /command trigger. Returns None if not found."""
    # Normalize: ensure leading slash
    if not command.startswith("/"):
        command = "/" + command
    command = command.lower().strip()

    for skill in load_skills():
        if skill["command"].lower().strip() == command:
            return skill
    return None


def list_skills() -> list[dict]:
    """Return a lightweight list of skills for the frontend (no step details)."""
    return [
        {
            "name": skill["name"],
            "command": skill["command"],
            "description": skill.get("description", ""),
            "inputs": skill.get("inputs", []),
        }
        for skill in load_skills()
    ]
