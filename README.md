# localAssist

A self-hosted AI assistant that runs on your own machine. A chat UI in the
browser drives an agent that can actually do things — search the web, work with
your files, remember what matters, and more — with your data staying local by
default.

It's local-first: point it at a model running locally through Ollama and
nothing leaves your machine. Cloud models (Groq, OpenRouter) are supported too
if you'd rather trade privacy for speed, and you can switch between them per
conversation.

## What it can do

- **Chat with tools** — the agent decides when to reach for a tool and streams
  its answer back as it works.
- **Web search** — looks things up and pulls in current information.
- **Files** — reads, searches, and edits files in a sandboxed workspace. Edits
  are proposed for you to approve before anything is written.
- **Memory** — remembers facts about you across conversations.
- **Plans** — breaks bigger requests into steps and tracks its progress.
- **News & weather** — quick current info without leaving the chat.
- **Email drafts** — opens a prefilled compose window.
- **Skills** — slash commands (`/briefing`, `/research`, `/notes`) that chain a
  few steps into one action.
- **Voice input** — talk to it instead of typing.
- **Semantic search** — finds things by meaning, not just keywords, across your
  workspace.

## Notes

There's a built-in handwriting notes app — write on a canvas, and it transcribes
your pages in the background using native Windows Ink handwriting recognition
(see `winink/`). The recognized text gets indexed alongside the rest of your
workspace, so the assistant can search and reference your handwritten notes. Recognition is fully local via the OS.

The Windows Ink tool is a small C# CLI. Build it once with
`pwsh winink/build.ps1`; the notes app invokes it automatically. See
`winink/README.md` for details.

## Local-first

The default posture is private: run a local model with Ollama and your prompts,
files, and memory never touch a third party. Web search and news reach out to
their APIs when you use those tools, but the model itself can stay entirely on
your machine. Cloud providers are there when you want them, not required.

## MCP servers

The assistant can pull in tools from external [Model Context Protocol](https://modelcontextprotocol.io)
servers alongside its built-in ones. Local (stdio) and remote (HTTP / SSE)
servers are both supported.

### Configuring servers

Servers go in `mcp_servers.json` at the repo root, using the same `mcpServers`
shape every other MCP client uses, so you can paste blocks straight in:

```json
{
  "mcpServers": {
    "time": {
      "command": "uvx",
      "args": ["mcp-server-time"]
    },
    "remote-example": {
      "url": "https://example.com/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

**Transport is inferred:**

- a `command` → **stdio** (a local process; `args` and `env` are passed through)
- a `url` → **http** (streamable HTTP, the default for remote)
- a `url` with `"type": "sse"` → **sse** (legacy Server-Sent Events override)

`headers` (remote auth) and `env` (stdio secrets) stay on the server — they're
never sent to the browser or written anywhere else.

### Enable / disable + status (the sidecar)

Per-server state lives next to the config in `mcp_meta.json`, keyed by the same
server name:

- `enabled` / `order` — yours to set (missing entries are auto-backfilled on
  load; set `"enabled": false` to keep a server configured but off).
- `last_status` / `last_error` / `tool_count` — written automatically after each
  connect, and surfaced in the UI.

`mcp_servers.json` stays paste-in friendly: add/delete from the UI edit only the
`mcpServers` object and leave any other top-level keys alone. Enable/disable and
all runtime status go in the sidecar, so the standard config only ever holds
real server blocks.

### Reloading from the UI

Open **Settings → MCP Servers** in the sidebar to see each configured server
with its transport, tool count, and connection status (disabled and failed
servers show up too, with their error). You can manage servers right there:

- **reload** — re-read both files and reconnect (the same routine that runs at
  startup).
- **+ add server** — a small form (stdio / http / sse) that writes the server to
  `mcp_servers.json` and reloads. `env` / `headers` are saved but never shown
  back.
- **on / off** per server — flips `enabled` in the sidecar only (the config file
  isn't touched), then reloads so a disabled server drops its tools.
- **✕** per server — removes it from `mcp_servers.json`, prunes its sidecar
  entry, and reloads.

Every change reloads automatically; new or removed tools take effect on the
**next turn** of each conversation (the KV cache resets for that turn; that's
expected).