// Injected into the active tab while the side panel is open. Reports the page's
// current text selection to the service worker, which relays it to the composer
// quote pill. Read-only and passive: never touches the page DOM or page events.
(() => {
  const KEY = "__localassistSelectionReader";

  // Guard against double-binding on re-injection (tab re-activated). The flag
  // lives on window so it persists across separate executeScript injections,
  // but is non-enumerable so page scripts don't see it in normal enumeration.
  const existing = window[KEY];
  if (existing) {
    try { existing.report(true); } catch (_) { /* ignore */ }
    return;
  }

  const DEBOUNCE_MS = 180;
  const MAX_CHARS = 8000;

  let debounceTimer = null;
  let lastSent = null;

  function currentSelectionText() {
    const sel = window.getSelection();
    if (!sel) return "";
    let text = sel.toString();
    if (!text) return "";
    text = text.replace(/\s+/g, " ").trim();
    return text.length > MAX_CHARS ? text.slice(0, MAX_CHARS) + "…" : text;
  }

  // `force` bypasses dedup — used on re-injection, where the worker already
  // cleared the pill so an unchanged selection must still be re-sent.
  function report(force) {
    const text = currentSelectionText();
    if (!force && text === lastSent) return;
    lastSent = text;
    try {
      chrome.runtime.sendMessage({
        type: "localassist:selection-update",
        text,
        url: location.href,
        title: document.title || "",
      });
    } catch (_) { /* extension context gone — harmless */ }
  }

  function scheduleReport() {
    if (debounceTimer) clearTimeout(debounceTimer);
    debounceTimer = setTimeout(report, DEBOUNCE_MS);
  }

  document.addEventListener("selectionchange", scheduleReport, { passive: true });

  Object.defineProperty(window, KEY, {
    value: { report },
    enumerable: false,
    configurable: true,
    writable: true,
  });

  report();
})();
