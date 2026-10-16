// Open the side panel directly when the toolbar icon is clicked.
chrome.runtime.onInstalled.addListener(() => {
  chrome.sidePanel
    .setPanelBehavior({ openPanelOnActionClick: true })
    .catch((err) => console.error("[localAssist] setPanelBehavior failed:", err));
});

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
