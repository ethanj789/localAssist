"""
mcp_infra/config.py — External MCP server config loader + metadata sidecar.

Two files, kept deliberately separate:

  1. The STANDARD config (`mcp_servers.json`, path overridable via
     MCP_SERVERS_CONFIG). This is the exact `mcpServers` object shape every
     other MCP client (Claude Desktop, Cursor, VS Code, ...) uses, so a server
     block copy-pasted from anywhere drops straight in:

         {
           "mcpServers": {
             "time":  { "command": "uvx", "args": ["mcp-server-time"] },
             "demo":  { "url": "https://example.com/mcp", "headers": {...} },
             "old":   { "url": "https://legacy/sse", "type": "sse" }
           }
         }

     Historically we never wrote this file (kept pristine for paste-in). The
     UI now edits it through the add/remove helpers below, which mutate ONLY the
     `mcpServers` object and write atomically, preserving any other top-level
     keys. Enable/disable never touches this file — it flips the sidecar.

  2. The METADATA sidecar (`mcp_meta.json`), keyed by the same server name,
     holding OUR runtime/UI state so the standard config stays clean:

         {
           "time": { "enabled": true, "order": 0,
                     "last_status": "ok", "last_error": null, "tool_count": 2 }
         }

     `enabled` / `order` are user-set (defaulted + backfilled when absent).
     `last_status` / `last_error` / `tool_count` are written by the connector
     after each refresh. This file IS written (atomic tmp-rename).

Transport is INFERRED from the standard block, matching how standard clients
behave:
    - `command` present              -> stdio
    - `url` present                  -> remote, streamable-HTTP by default
    - `url` + `type`/`transport`=sse -> remote SSE (legacy override)

The in-memory parsed records returned by `load_enabled_servers()` are the input
to the connector's refresh. Files are read only during refresh — never per
request.
"""
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# This module lives in mcp_infra/, but the JSON config/sidecar stay at the repo
# root (they're user-editable, kept alongside events_cache.json / model_settings
# .json), so go up one level from this file to find them.
ROOT_DIR = Path(__file__).resolve().parent.parent

CONFIG_PATH = Path(os.getenv("MCP_SERVERS_CONFIG", str(ROOT_DIR / "mcp_servers.json")))
META_PATH = ROOT_DIR / "mcp_meta.json"


@dataclass
class ServerRecord:
    """A single resolved external MCP server: standard config fields merged with
    sidecar metadata, plus the inferred transport."""
    name: str
    transport: str                      # "stdio" | "http" | "sse"
    # stdio
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] | None = None
    # remote (http / sse)
    url: str | None = None
    headers: dict[str, str] | None = None
    # metadata
    enabled: bool = True
    order: int = 0

    def sanitized(self, meta: dict | None = None) -> dict:
        """UI-safe view: no secret-bearing fields (`headers`, `env`). Includes
        connector-written status from the sidecar meta entry if provided."""
        meta = meta or {}
        return {
            "name": self.name,
            "transport": self.transport,
            "enabled": self.enabled,
            "order": self.order,
            "command": self.command,
            "args": list(self.args),
            "url": self.url,
            "last_status": meta.get("last_status"),
            "last_error": meta.get("last_error"),
            "tool_count": meta.get("tool_count"),
        }


# ── file IO ──────────────────────────────────────────────────────────────────

def _read_json(path: Path) -> dict:
    try:
        if path.exists():
            # utf-8-sig tolerates a BOM, which some Windows editors (and
            # PowerShell's Set-Content -Encoding utf8) prepend; plain utf-8
            # would choke on it.
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                return data
            log.warning("mcp config %s is not a JSON object; ignoring", path)
    except Exception as exc:
        log.warning("mcp config read failed (%s): %s", path, exc)
    return {}


def _atomic_write_json(path: Path, data: dict) -> None:
    """Atomic tmp-file write, mirroring tools/events.py _save_cache."""
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:
        log.warning("mcp meta write failed (%s): %s", path, exc)


def load_raw_config() -> dict:
    """Return the raw `mcpServers` object (name -> block), or empty if absent."""
    data = _read_json(CONFIG_PATH)
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        if data:
            log.warning("mcp config %s has no 'mcpServers' object", CONFIG_PATH)
        return {}
    return servers


def load_meta() -> dict:
    """Return the sidecar metadata object (name -> meta dict)."""
    return _read_json(META_PATH)


def save_meta(meta: dict) -> None:
    _atomic_write_json(META_PATH, meta)


# ── transport inference ────────────────────────────────────────────────────────

def _infer_transport(block: dict) -> str | None:
    """Infer the transport from a standard server block. Returns None if the
    block is malformed (neither `command` nor `url`)."""
    if block.get("command"):
        return "stdio"
    if block.get("url"):
        override = str(block.get("type") or block.get("transport") or "").strip().lower()
        if override == "sse":
            return "sse"
        return "http"
    return None


def _parse_block(name: str, block: dict, meta_entry: dict) -> ServerRecord | None:
    """Build a ServerRecord from one standard block + its sidecar meta entry.
    Returns None (and logs) if the block is malformed."""
    if not isinstance(block, dict):
        log.warning("mcp server %r block is not an object; skipping", name)
        return None
    transport = _infer_transport(block)
    if transport is None:
        log.warning("mcp server %r has neither 'command' nor 'url'; skipping", name)
        return None

    enabled = bool(meta_entry.get("enabled", True))
    order = meta_entry.get("order", 0)
    try:
        order = int(order)
    except (TypeError, ValueError):
        order = 0

    rec = ServerRecord(name=name, transport=transport, enabled=enabled, order=order)
    if transport == "stdio":
        rec.command = str(block["command"])
        args = block.get("args", [])
        rec.args = [str(a) for a in args] if isinstance(args, list) else []
        env = block.get("env")
        rec.env = {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else None
    else:
        rec.url = str(block["url"])
        headers = block.get("headers")
        rec.headers = {str(k): str(v) for k, v in headers.items()} if isinstance(headers, dict) else None
    return rec


# ── public API ──────────────────────────────────────────────────────────────

def _default_meta_entry(order: int) -> dict:
    return {"enabled": True, "order": order, "last_status": None, "last_error": None, "tool_count": None}


def backfill_meta(config: dict, meta: dict) -> tuple[dict, bool]:
    """Ensure every server in `config` has a sidecar meta entry. Returns the
    (possibly updated) meta and whether anything changed. Does NOT prune meta
    entries that have no matching config server — the caller may keep them, but
    `load_enabled_servers` ignores orphans."""
    changed = False
    # Assign default order by config insertion order for servers missing one.
    next_order = 0
    for existing in meta.values():
        if isinstance(existing, dict) and isinstance(existing.get("order"), int):
            next_order = max(next_order, existing["order"] + 1)

    for name in config:
        entry = meta.get(name)
        if not isinstance(entry, dict):
            meta[name] = _default_meta_entry(next_order)
            next_order += 1
            changed = True
        else:
            # Backfill any missing keys without clobbering user-set values.
            for k, v in _default_meta_entry(entry.get("order", next_order)).items():
                if k not in entry:
                    entry[k] = v
                    changed = True
    return meta, changed


def load_all_servers() -> tuple[list[ServerRecord], dict]:
    """Parse EVERY configured server (enabled or not), backfilling the sidecar.
    Returns (records, meta). Records are sorted by (order, name). The sidecar is
    persisted if backfill added anything. Malformed blocks are skipped."""
    config = load_raw_config()
    meta = load_meta()
    meta, changed = backfill_meta(config, meta)
    if changed:
        save_meta(meta)

    records: list[ServerRecord] = []
    for name, block in config.items():
        rec = _parse_block(name, block, meta.get(name, {}))
        if rec is not None:
            records.append(rec)
    records.sort(key=lambda r: (r.order, r.name))
    return records, meta


def load_enabled_servers() -> list[ServerRecord]:
    """The connector's input: only enabled servers, sorted by (order, name)."""
    records, _ = load_all_servers()
    return [r for r in records if r.enabled]


def write_status(name: str, *, status: str, error: str | None = None, tool_count: int | None = None) -> None:
    """Write connector status back to the sidecar for one server. Reads the
    current sidecar, updates the one entry, and atomically rewrites. Leaves the
    pristine standard config untouched."""
    meta = load_meta()
    entry = meta.get(name)
    if not isinstance(entry, dict):
        entry = _default_meta_entry(0)
    entry["last_status"] = status
    entry["last_error"] = error
    entry["tool_count"] = tool_count
    meta[name] = entry
    save_meta(meta)


# ── UI-driven config mutations (add / remove / toggle) ────────────────────────
#
# These are the write side the reload UI needs. `write_status` above and
# `save_meta` are best-effort (log and swallow). These, by contrast, raise
# ConfigWriteError on failure so the HTTP layer can surface a real error instead
# of silently losing a user's add. All three re-use the same read-modify-write
# shape and leave unrelated top-level config keys untouched.

class ConfigWriteError(Exception):
    """Raised when a UI-driven config/sidecar write fails or is rejected."""


def _atomic_write_json_strict(path: Path, data: dict) -> None:
    """Like `_atomic_write_json` but raises on failure (for UI writes that must
    not fail silently)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _normalize_block(block: dict) -> dict:
    """Validate + canonicalize a single server block the UI submitted. Keeps
    only recognized keys, coerces types, and verifies it names a transport
    (command or url). Raises ConfigWriteError if malformed."""
    if not isinstance(block, dict):
        raise ConfigWriteError("server block must be an object")

    out: dict = {}
    command = block.get("command")
    url = block.get("url")

    if command:
        out["command"] = str(command)
        args = block.get("args")
        if isinstance(args, list) and args:
            out["args"] = [str(a) for a in args]
        env = block.get("env")
        if isinstance(env, dict) and env:
            out["env"] = {str(k): str(v) for k, v in env.items()}
    elif url:
        out["url"] = str(url)
        # Preserve the SSE override; http is the default and needs no marker.
        override = str(block.get("type") or block.get("transport") or "").strip().lower()
        if override == "sse":
            out["type"] = "sse"
        headers = block.get("headers")
        if isinstance(headers, dict) and headers:
            out["headers"] = {str(k): str(v) for k, v in headers.items()}
    else:
        raise ConfigWriteError("server needs either a 'command' (stdio) or a 'url' (remote)")

    return out


def add_server(name: str, block: dict, *, overwrite: bool = False) -> None:
    """Add (or overwrite) a server block in `mcp_servers.json` and ensure a
    sidecar entry exists for it. Validates the block; raises ConfigWriteError on
    a bad name, a duplicate (unless overwrite), or a write failure."""
    name = (name or "").strip()
    if not name:
        raise ConfigWriteError("server name is required")

    normalized = _normalize_block(block)

    config = load_raw_config()
    if name in config and not overwrite:
        raise ConfigWriteError(f"a server named {name!r} already exists")
    config[name] = normalized

    data = _read_json(CONFIG_PATH)
    if not isinstance(data, dict):
        data = {}
    data["mcpServers"] = config
    try:
        _atomic_write_json_strict(CONFIG_PATH, data)
    except Exception as exc:
        raise ConfigWriteError(f"failed to write config: {exc}") from exc

    # Give the new server a sidecar entry so it's immediately enabled and
    # ordered after existing ones. backfill_meta assigns the next order.
    meta = load_meta()
    meta, _ = backfill_meta(config, meta)
    save_meta(meta)


def remove_server(name: str) -> None:
    """Delete a server from `mcp_servers.json` and prune its sidecar entry.
    Raises ConfigWriteError if the server isn't configured or the write fails."""
    name = (name or "").strip()
    if not name:
        raise ConfigWriteError("server name is required")

    config = load_raw_config()
    if name not in config:
        raise ConfigWriteError(f"no server named {name!r}")
    config.pop(name, None)

    data = _read_json(CONFIG_PATH)
    if not isinstance(data, dict):
        data = {}
    data["mcpServers"] = config
    try:
        _atomic_write_json_strict(CONFIG_PATH, data)
    except Exception as exc:
        raise ConfigWriteError(f"failed to write config: {exc}") from exc

    # Prune the orphaned sidecar entry too (keeps the two files in step).
    meta = load_meta()
    if name in meta:
        meta.pop(name, None)
        save_meta(meta)


def set_enabled(name: str, enabled: bool) -> None:
    """Flip a server's `enabled` flag in the sidecar ONLY — the standard config
    is never touched. Raises ConfigWriteError if the server isn't configured."""
    name = (name or "").strip()
    if not name:
        raise ConfigWriteError("server name is required")

    config = load_raw_config()
    if name not in config:
        raise ConfigWriteError(f"no server named {name!r}")

    meta = load_meta()
    entry = meta.get(name)
    if not isinstance(entry, dict):
        # Backfill a default entry (assigns order) then flip it.
        meta, _ = backfill_meta(config, meta)
        entry = meta.get(name, _default_meta_entry(0))
    entry["enabled"] = bool(enabled)
    meta[name] = entry
    try:
        _atomic_write_json_strict(META_PATH, meta)
    except Exception as exc:
        raise ConfigWriteError(f"failed to write sidecar: {exc}") from exc
