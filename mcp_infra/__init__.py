"""
mcp_infra — the MCP subsystem for localAssist.

Everything that speaks or wires up the Model Context Protocol lives here, split
from the agent's own tool *implementations* (which stay in `tools/`).

Modules:
    server      — local MCP server entry point (spawned as a stdio subprocess;
                  runs tools.registry.app). Launched via CONFIG["mcp_server_cmd"].
    client      — MCPClient: hand-rolled JSON-RPC-over-stdio client for the
                  local server. Unchanged, synchronous, thread-based.
    config      — standard `mcpServers` config loader + `mcp_meta.json` sidecar
                  (enabled/order + connector-written status). Transport inference.
    external    — ExternalMCP: connector over the mcp SDK's ClientSessionGroup
                  for external local-stdio / remote HTTP-SSE servers. Prefixes
                  their tools `servername__tool`.
    aggregator  — MCPAggregator: the one object the agent talks to. Fronts the
                  local client + external connector behind list_tools()/call_tool(),
                  merging on list and demuxing on call.

Note: this package is deliberately NOT named `mcp` to avoid shadowing the
installed `mcp` SDK package.
"""

from mcp_infra.client import MCPClient
from mcp_infra.aggregator import MCPAggregator

__all__ = ["MCPClient", "MCPAggregator"]
