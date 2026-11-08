"""
mcp_infra/external.py — External MCP server connector.

Wraps the mcp SDK's `ClientSessionGroup` (a multiplexer that can hold many
sessions over stdio / SSE / streamable-HTTP at once) and presents the SAME
narrow async surface the agent already uses against the local client:

    list_tools() -> list[dict]        # {name, description, inputSchema}
    call_tool(name, args) -> str      # flattened newline-joined text

Plus lifecycle:

    refresh()   # re-read config, (re)connect enabled servers, drop the rest
    aclose()    # tear everything down

Design notes:
  - Every external tool is namespaced `servername__tool` via the group's
    `component_name_hook`, so it can never collide with an unprefixed built-in
    local tool, and the aggregator can route by that prefix.
  - Sessions are long-lived async contexts bound to the app event loop; the
    group is held in an AsyncExitStack opened once and closed at shutdown. We
    rebuild the whole group on `refresh()` (simplest correct approach — the KV
    reset on tool-list change is accepted project-wide) rather than surgically
    diffing sessions, since a full teardown/rebuild is robust and refresh is a
    rare, explicit action (startup + /mcp/reload), never per request.
  - Per-server connection failures are isolated: one bad server logs, records
    its error to the sidecar, and does not stop the others.
  - Results from the SDK (`types.CallToolResult`) are flattened to the same
    plain newline-joined text string the local MCPClient returns.
"""
import logging
import os
import shutil
import sys
from datetime import timedelta
from pathlib import Path

from mcp import types
from mcp.client.session_group import (
    ClientSessionGroup,
    SseServerParameters,
    StreamableHttpParameters,
)
from mcp.client.stdio import StdioServerParameters

from mcp_infra import config as mcp_config

log = logging.getLogger(__name__)

# Separator between the server name and the tool name in the aggregated,
# prefixed tool id. Chosen as "__" so it reads cleanly and is unlikely to appear
# in a built-in tool name (all current built-ins are single snake_case words).
NAME_SEP = "__"


def _resolve_command(command: str) -> str:
    """Resolve a stdio server `command` to an absolute path when it's a bare
    name (e.g. "uvx", "npx").

    Why this is needed: the SDK spawns the server as a child of THIS process,
    and the child executable is looked up on this process's PATH. When the app
    runs via `.venv\\Scripts\\python.exe` directly (not an activated venv), the
    venv's Scripts/bin dir is NOT on PATH, so a tool installed only in the venv
    (like `uvx`) can't be found -> WinError 2 / FileNotFoundError.

    We search, in order: an explicit path the user already gave (returned as-is),
    the venv's Scripts (Windows) / bin (POSIX) dir next to the running
    interpreter, and finally the normal PATH via shutil.which. If nothing
    matches we return the original string and let the SDK try (and surface a
    clean per-server error if it still can't find it).
    """
    if not command:
        return command
    # An explicit path (absolute, or containing a separator) is used verbatim.
    if os.path.isabs(command) or ("/" in command) or ("\\" in command):
        return command

    # 1) The venv Scripts/bin dir alongside the current interpreter.
    scripts_dir = Path(sys.executable).parent  # ...\.venv\Scripts or .../bin
    exts = [".exe", ".cmd", ".bat", ""] if os.name == "nt" else [""]
    for ext in exts:
        candidate = scripts_dir / f"{command}{ext}"
        if candidate.is_file():
            return str(candidate)

    # 2) Normal PATH lookup (covers system-wide installs).
    found = shutil.which(command)
    if found:
        return found

    # 3) Fall back to the bare name; the SDK will try and may still fail, which
    # we isolate and report per server.
    return command


def _params_for(rec: "mcp_config.ServerRecord"):
    """Map a parsed ServerRecord to the SDK's transport-specific params."""
    if rec.transport == "stdio":
        return StdioServerParameters(
            command=_resolve_command(rec.command or ""),
            args=list(rec.args),
            env=rec.env,
        )
    if rec.transport == "sse":
        return SseServerParameters(url=rec.url or "", headers=rec.headers)
    # default / "http"
    return StreamableHttpParameters(
        url=rec.url or "",
        headers=rec.headers,
        timeout=timedelta(seconds=30),
    )


class ExternalMCP:
    """Holds a ClientSessionGroup of external servers and exposes the agent's
    tool interface over their aggregated tools."""

    def __init__(self):
        self._group: ClientSessionGroup | None = None
        # name (prefixed, e.g. "time__get_current_time") -> tool dict
        self._tools: dict[str, dict] = {}
        self._current_connect_name: str | None = None

    # ── lifecycle ──────────────────────────────────────────────────────────

    async def open(self) -> None:
        """Open an empty group, then connect whatever config enables. Safe to
        call once at startup; `refresh()` is what (re)loads servers."""
        await self.refresh()

    async def aclose(self) -> None:
        await self._teardown_group()

    async def _teardown_group(self) -> None:
        if self._group is not None:
            try:
                await self._group.__aexit__(None, None, None)
            except Exception as exc:  # pragma: no cover - best-effort teardown
                log.warning("external mcp teardown error: %s", exc)
        self._stack = None
        self._group = None
        self._tools = {}

    # ── refresh (startup + /mcp/reload share this) ───────────────────────────

    async def refresh(self) -> None:
        """Re-read config and rebuild the session group from scratch. Isolates
        per-server failures and records status to the sidecar."""
        # Tear down any existing group first so we never leak sessions/processes
        # across a reload.
        await self._teardown_group()

        records = mcp_config.load_enabled_servers()

        # Let the group own its own exit stack (no exit_stack arg). Entering it
        # as an async context means __aexit__ (in _teardown_group) closes every
        # session's process/connection.
        group = ClientSessionGroup(component_name_hook=self._name_hook)
        await group.__aenter__()

        tools: dict[str, dict] = {}
        for rec in records:
            # Set during this connect so the name hook knows which server the
            # tools belong to (the SDK passes server_info, but we key off our
            # own config name for a stable, user-chosen prefix).
            self._current_connect_name = rec.name
            try:
                await group.connect_to_server(_params_for(rec))
            except Exception as exc:
                log.warning("external mcp: server %r failed to connect: %s", rec.name, exc)
                mcp_config.write_status(rec.name, status="error", error=str(exc), tool_count=0)
                continue
            # Count this server's tools (those now in the group carrying our
            # prefix) for the status line.
            prefix = rec.name + NAME_SEP
            count = sum(1 for n in group.tools if n.startswith(prefix))
            mcp_config.write_status(rec.name, status="ok", error=None, tool_count=count)
            log.info("external mcp: connected %r (%s) | %d tools", rec.name, rec.transport, count)

        self._current_connect_name = None

        # Snapshot the aggregated tools into our plain-dict cache (the in-memory
        # source of truth the aggregator reads per request).
        for name, tool in group.tools.items():
            tools[name] = {
                "name": name,
                "description": tool.description or "",
                "inputSchema": tool.inputSchema or {"type": "object", "properties": {}},
            }

        self._group = group
        self._tools = tools

    def _name_hook(self, name: str, server_info: types.Implementation) -> str:
        """Prefix every external tool with its (config) server name. Falls back
        to the server's self-reported name if we somehow lack a connect name."""
        base = getattr(self, "_current_connect_name", None) or (
            server_info.name if server_info else "ext"
        )
        return f"{base}{NAME_SEP}{name}"

    # ── agent-facing interface ───────────────────────────────────────────────

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    def list_tools(self) -> list[dict]:
        return list(self._tools.values())

    async def call_tool(self, name: str, arguments: dict) -> str:
        """Route to the owning session via the group, flatten the result to the
        same plain newline-joined text the local MCPClient returns."""
        if self._group is None or name not in self._tools:
            return f"Tool error: unknown external tool {name!r}"
        try:
            result = await self._group.call_tool(name, arguments or {})
        except Exception as exc:
            return f"Tool error: {exc}"
        return self._flatten(result)

    @staticmethod
    def _flatten(result: types.CallToolResult) -> str:
        parts: list[str] = []
        for item in result.content or []:
            text = getattr(item, "text", None)
            if text is not None:
                parts.append(text)
        return "\n".join(parts)
