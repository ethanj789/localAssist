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

## Files

- `manifest.json` — MV3 manifest, declares the side panel and permissions.
- `background.js` — makes the toolbar icon open the side panel.
- `sidepanel.html` / `.css` / `.js` — the panel: an iframe plus an offline
  fallback that probes `/startup-token` before loading.
- `gen_icons.py` — regenerates the PNG icons (pure Python, no Pillow).

## Notes

- Voice input uses the browser Web Speech API, which is allowed on `localhost`,
  so it keeps working inside the panel.
- CORS is already open on the server (`allow_origins=["*"]`), and the server
  sends no `X-Frame-Options`, so embedding works without server changes.
