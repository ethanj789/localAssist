"""
mcp_infra/aggregator.py — The one object the agent talks to.

Fronts TWO tool sources behind the exact async interface the agent already used
against the raw MCPClient (`list_tools()` / `call_tool(name, args)`), so it is a
drop-in replacement in server.py — nothing in agent/ changes:

    LOCAL  : the existing hand-rolled MCPClient (unchanged stdio subprocess
             running tools.registry.app). Its tools stay UNPREFIXED.
    EXTERNAL: ExternalMCP, a ClientSessionGroup of community/remote servers.
             Its tools are prefixed `servername__tool`.

MERGE on list:  local tools first (unprefixed), then external (prefixed).
DEMUX on call:  a name the external connector knows -> external; everything
                else -> local MCPClient (preserving its error-string behavior).

The merged in-memory tool list is the source of truth for every request. The
JSON config/sidecar are read ONLY during refresh() (startup + POST /mcp/reload),
never per request.

Local-first: if the external connector fails to open/refresh, local tools still
serve — the agent keeps working with exactly today's capabilities.
"""
import logging

from mcp_infra.external import ExternalMCP

log = logging.getLogger(__name__)


class MCPAggregator:
    def __init__(self, local_client):
        # The existing MCPClient instance (constructed in server.py). Left
        # entirely unmanaged by us except start/stop, which we delegate.
        self._local = local_client
        self._external = ExternalMCP()

    # ── lifecycle ──────────────────────────────────────────────────────────

    async def open(self) -> None:
        """Start the local client (its existing synchronous .start, run off the
        event loop) then open + refresh external servers. External failure is
        non-fatal (local-first)."""
        import asyncio
        await asyncio.get_event_loop().run_in_executor(None, self._local.start)
        try:
            await self._external.open()
        except Exception as exc:
            log.warning("external mcp open failed (local tools still available): %s", exc)

    async def aclose(self) -> None:
        try:
            await self._external.aclose()
        except Exception as exc:  # pragma: no cover
            log.warning("external mcp close error: %s", exc)
        import asyncio
        await asyncio.get_event_loop().run_in_executor(None, self._local.stop)

    async def refresh(self) -> None:
        """Re-read config and rebuild external servers. The local client is
        untouched. Raises nothing fatal — logs and keeps local tools on error."""
        try:
            await self._external.refresh()
        except Exception as exc:
            log.warning("external mcp refresh failed (local tools unaffected): %s", exc)

    # ── agent-facing interface (identical to the old MCPClient surface) ──────

    async def list_tools(self) -> list[dict]:
        """Merged tool list: local (unprefixed) first, then external (prefixed).
        Local is fetched live from the local client each call, matching the old
        behavior; external is served from the connector's in-memory snapshot."""
        local = await self._local.list_tools()
        external = self._external.list_tools()
        return list(local) + list(external)

    async def call_tool(self, name: str, arguments: dict) -> str:
        """Demux: external connector owns the name -> external; else -> local."""
        if self._external.has_tool(name):
            return await self._external.call_tool(name, arguments)
        return await self._local.call_tool(name, arguments)

    # ── introspection for the reload/read endpoints ──────────────────────────

    def external_status(self) -> list[dict]:
        """Sanitized per-server view (name/transport/enabled/order/status) for
        the UI endpoints. No secret-bearing fields. Reflects the current sidecar
        + parsed config, so it shows disabled/failed servers too."""
        from mcp_infra import config as mcp_config
        records, meta = mcp_config.load_all_servers()
        return [r.sanitized(meta.get(r.name, {})) for r in records]
