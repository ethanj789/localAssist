# localAssist Chrome Sidebar Extension

Loads your self-hosted localAssist chat UI into the Chrome **side panel**, so
the assistant rides along next to whatever you're browsing.

It works by embedding your running local server (`http://localhost:8000` by
default) in an iframe inside the side panel. No copy of the UI lives in the
extension — it always shows the live app from your server, so updates to the
app show up automatically.

## Requirements

- Chrome 114+ (side panel API)
- Your localAssist server running locally (`python server.py`)

## Install (unpacked)

1. Start the assistant server:
   ```
   python server.py
   ```
2. Open `chrome://extensions` in Chrome.
3. Toggle **Developer mode** on (top right).
4. Click **Load unpacked** and select this `chrome-extension` folder.
5. Pin the extension, then click its toolbar icon to open the side panel.

## Changing the server address

If your server runs on a different host or port, open the side panel while the
server is unreachable (or just stop it once). The fallback screen lets you type
a new address and hit **Retry**. The address is saved and reused next time.

You can also edit `DEFAULT_URL` in `sidepanel.js`.

## Highlight-to-quote

While the side panel is open, highlighting text on the page you're browsing
arms a **quote** in the chat composer — a removable pill (like an attachment)
that rides along with your next message as quoted context. It follows the
active tab: switch tabs and the pill reflects that tab's current selection;
clear the selection and the pill clears.

How it works (and why it's unobtrusive):

- The panel tells the service worker when it's open. Only then does the worker
  inject a tiny reader (`selection-reader.js`) into the **active tab**, and
  re-inject on tab switch / navigation. If you never open the panel, nothing is
  ever injected into any page.
- The reader is **read-only and passive**: it reads `getSelection()` on a
  debounced `selectionchange`, never touches the page DOM, adds no elements,
  and sets no page globals — so a site has no observable trace of it.
- Restricted pages (`chrome://`, the web store, etc.) can't be injected; the
  feature just no-ops there.
- The selection is relayed worker → panel → iframe via the same strict-origin
  `postMessage` the screenshot uses, then staged as a `quote`-type attachment
  (text only, no file on disk).

## Files

- `manifest.json` — MV3 manifest, declares the side panel and permissions
  (`sidePanel`, `storage`, `tabs`, `scripting`).
- `background.js` — opens the side panel on icon click, serves tab screenshots,
  and manages selection-reader injection + relay for highlight-to-quote.
- `selection-reader.js` — read-only selection reporter injected into the active
  tab on demand (never bundled into pages ahead of time).
- `sidepanel.html` / `.css` / `.js` — the panel: an iframe plus an offline
  fallback that probes `/startup-token` before loading; also bridges selection
  messages into the app.
- `gen_icons.py` — regenerates the PNG icons (pure Python, no Pillow).

## Notes

- Voice input uses the browser Web Speech API, which is allowed on `localhost`,
  so it keeps working inside the panel.
- CORS is already open on the server (`allow_origins=["*"]`), and the server
  sends no `X-Frame-Options`, so embedding works without server changes.
