const DEFAULT_URL = "http://localhost:8000";

const frame = document.getElementById("assistant-frame");
const offline = document.getElementById("offline");
const urlInput = document.getElementById("server-url");
const retryBtn = document.getElementById("retry-btn");
const hint = document.getElementById("offline-hint");
const toolbarZone = document.getElementById("toolbar-zone");
const screenshotBtn = document.getElementById("screenshot-btn");

// The origin the assistant iframe is currently loaded from. Used as the strict
// postMessage target so we never leak a screenshot to another origin.
let activeOrigin = null;

// Saved server URL, or the default if none has been stored yet.
function getServerUrl() {
  return new Promise((resolve) => {
    chrome.storage.sync.get({ serverUrl: DEFAULT_URL }, (data) => {
      resolve((data.serverUrl || DEFAULT_URL).replace(/\/+$/, ""));
    });
  });
}

function saveServerUrl(url) {
  return new Promise((resolve) => {
    chrome.storage.sync.set({ serverUrl: url }, resolve);
  });
}

// Probe a lightweight endpoint to confirm the server is reachable.
async function isServerUp(baseUrl) {
  try {
    const res = await fetch(`${baseUrl}/startup-token`, {
      method: "GET",
      cache: "no-store",
    });
    return res.ok;
  } catch {
    return false;
  }
}

function showAssistant(baseUrl) {
  frame.src = baseUrl;
  frame.classList.remove("hidden");
  offline.classList.add("hidden");
  try {
    activeOrigin = new URL(baseUrl).origin;
  } catch {
    activeOrigin = null;
  }
  if (toolbarZone) toolbarZone.classList.remove("hidden");
}

function showOffline(baseUrl, message) {
  frame.classList.add("hidden");
  offline.classList.remove("hidden");
  urlInput.value = baseUrl;
  hint.textContent = message || "";
  activeOrigin = null;
  if (toolbarZone) toolbarZone.classList.add("hidden");
}

// Ask the service worker to screenshot the visible browser tab, then hand the
// resulting PNG data URL to the assistant app inside the iframe. The app treats
// it exactly like a pasted image.
function captureAndAttach() {
  if (!activeOrigin) return;
  if (screenshotBtn) screenshotBtn.disabled = true;

  chrome.runtime.sendMessage({ type: "localassist:capture-tab" }, (resp) => {
    if (screenshotBtn) screenshotBtn.disabled = false;

    if (chrome.runtime.lastError) {
      console.error("[localAssist] capture error:", chrome.runtime.lastError);
      return;
    }
    if (!resp || !resp.ok || !resp.dataUrl) {
      console.error("[localAssist] capture failed:", resp && resp.error);
      return;
    }
    // Deliver to the iframe, scoped strictly to the loaded server origin.
    frame.contentWindow.postMessage(
      { type: "localassist:screenshot", dataUrl: resp.dataUrl },
      activeOrigin
    );
  });
}

if (screenshotBtn) {
  screenshotBtn.addEventListener("click", captureAndAttach);
}

async function connect() {
  hint.textContent = "";
  const baseUrl = (urlInput.value || (await getServerUrl())).replace(/\/+$/, "");
  const up = await isServerUp(baseUrl);
  if (up) {
    await saveServerUrl(baseUrl);
    showAssistant(baseUrl);
  } else {
    showOffline(baseUrl, "No response. Is the server running on this address?");
  }
}

retryBtn.addEventListener("click", connect);
urlInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") connect();
});

(async function init() {
  const baseUrl = await getServerUrl();
  urlInput.value = baseUrl;
  const up = await isServerUp(baseUrl);
  if (up) {
    showAssistant(baseUrl);
  } else {
    showOffline(baseUrl);
  }
})();
