// Open the side panel directly when the toolbar icon is clicked.
chrome.runtime.onInstalled.addListener(() => {
  chrome.sidePanel
    .setPanelBehavior({ openPanelOnActionClick: true })
    .catch((err) => console.error("[localAssist] setPanelBehavior failed:", err));
});

// ── Tab screenshot ──────────────────────────────────────────────────────────
// Capture a screenshot of the currently visible browser tab and return it as a
// PNG data URL. The side panel asks for this via chrome.runtime.sendMessage
// because captureVisibleTab must run in the extension (service worker) context,
// not inside the iframe'd web app.
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg || msg.type !== "localassist:capture-tab") return;

  // Find the active tab in the last-focused normal window (the page the user is
  // browsing), then capture that tab's window. We avoid capturing the side
  // panel itself by targeting the tab's own windowId.
  chrome.tabs.query({ active: true, lastFocusedWindow: true }, (tabs) => {
    const tab = tabs && tabs[0];
    if (!tab) {
      sendResponse({ ok: false, error: "No active tab to capture." });
      return;
    }
    chrome.tabs.captureVisibleTab(tab.windowId, { format: "png" }, (dataUrl) => {
      if (chrome.runtime.lastError || !dataUrl) {
        sendResponse({
          ok: false,
          error:
            (chrome.runtime.lastError && chrome.runtime.lastError.message) ||
            "Capture failed.",
        });
        return;
      }
      sendResponse({ ok: true, dataUrl });
    });
  });

  // Keep the message channel open for the async sendResponse above.
  return true;
});

// ── Tab-selection → quote pill ───────────────────────────────────────────────
// Inject a read-only selection reader into the active tab while the panel is
// open, relay its reports to the panel, and re-inject on tab switch so the
// quote pill follows the active tab. Nothing is injected unless a panel is open.

let panelOpen = false;

// Skip pages Chrome won't inject into (chrome://, web store, etc.).
function isInjectableUrl(url) {
  if (!url) return false;
  return /^https?:/i.test(url) || /^file:/i.test(url);
}

// Safe to call repeatedly: the reader guards against double-binding.
function injectReader(tabId, url) {
  if (!panelOpen || tabId == null || !isInjectableUrl(url)) return;
  chrome.scripting
    .executeScript({ target: { tabId }, files: ["selection-reader.js"] })
    .catch(() => { /* restricted page or tab gone — harmless */ });
}

function injectActiveTab() {
  if (!panelOpen) return;
  chrome.tabs.query({ active: true, lastFocusedWindow: true }, (tabs) => {
    const tab = tabs && tabs[0];
    if (tab && tab.id != null) injectReader(tab.id, tab.url);
  });
}

// On tab switch: clear any stale pill first, then re-inject so the new tab's
// reader re-reports its selection (or empty, which keeps it clear).
chrome.tabs.onActivated.addListener(({ tabId }) => {
  if (!panelOpen) return;
  relayToPanels({ type: "localassist:selection-clear" });
  chrome.tabs.get(tabId, (tab) => {
    if (chrome.runtime.lastError || !tab) return;
    injectReader(tab.id, tab.url);
  });
});

// A navigation replaces the document (and its reader); re-inject the active tab.
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (!panelOpen) return;
  if (changeInfo.status !== "complete") return;
  if (!tab || !tab.active) return;
  injectReader(tabId, tab.url);
});

function relayToPanels(message) {
  chrome.runtime.sendMessage(message).catch(() => {});
}

chrome.runtime.onMessage.addListener((msg, sender, _sendResponse) => {
  if (!msg || typeof msg.type !== "string") return;

  switch (msg.type) {
    case "localassist:panel-open":
      panelOpen = true;
      injectActiveTab();
      return;

    case "localassist:panel-closed":
      panelOpen = false;
      return;

    // Only relay from a real tab (sender.tab present) that is the active tab —
    // a background tab's report must not clobber the pill.
    case "localassist:selection-update": {
      const fromTab = sender && sender.tab;
      if (!fromTab || !fromTab.active) return;
      relayToPanels({
        type: "localassist:selection",
        text: typeof msg.text === "string" ? msg.text : "",
        url: msg.url || fromTab.url || "",
        title: msg.title || fromTab.title || "",
        source: "tab-highlight",
      });
      return;
    }

    default:
      return;
  }
});
