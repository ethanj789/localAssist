import { API } from '../constants.js';
import { setStatus, showToast, setInputDisplay } from '../utils/dom.js';

export let currentUseProvider = 'ollama';

// Whether provider switching is currently locked (i.e. a chat is active).
export let providerLocked = false;

/**
 * Reflect a provider ("ollama" | "openrouter" | "groq") in the UI toggles
 * without hitting the backend. Used both on load (default local) and when
 * resuming a conversation with a stored provider.
 */
export function reflectProvider(provider) {
    const isCloud = provider !== 'ollama';
    const useCloudEl = document.getElementById('cfg-use-cloud');
    if (useCloudEl) useCloudEl.checked = isCloud;
    currentUseProvider = provider;
    syncThinkingRowVisibility(isCloud);
}

/**
 * Lock or unlock provider switching. Once a conversation is active we don't
 * allow changing the provider — the backend pins each conversation to the
 * provider it was created with, so switching mid-chat is meaningless (and used
 * to wipe history). Locking disables the cloud chip + the sidebar checkbox.
 */
export function setProviderLocked(locked) {
    providerLocked = locked;
    const cloudChip = document.getElementById('chip-cloud-toggle');
    const useCloudEl = document.getElementById('cfg-use-cloud');
    if (cloudChip) {
        cloudChip.classList.toggle('disabled', locked);
        cloudChip.setAttribute('aria-disabled', locked ? 'true' : 'false');
        cloudChip.title = locked
            ? 'Provider is locked for this chat — start a new chat to switch'
            : 'Toggle cloud/local model';
    }
    if (useCloudEl) useCloudEl.disabled = locked;
}

function syncThinkingRowVisibility(isCloud) {
    const row = document.getElementById('ollama-thinking-row');
    if (row) row.style.display = isCloud ? 'none' : 'flex';
    // Sync chip toggles in the input bar
    const thinkingChip = document.getElementById('chip-thinking-toggle');
    if (thinkingChip) thinkingChip.style.display = isCloud ? 'none' : 'inline-flex';
    const ollamaModelChip = document.getElementById('ollama-model-select-wrapper');
    if (ollamaModelChip) ollamaModelChip.style.display = isCloud ? 'none' : '';
    const modeSelector = document.getElementById('model-select-wrapper');
    if (modeSelector) modeSelector.style.display = isCloud ? '' : 'none';
    const cloudChip = document.getElementById('chip-cloud-toggle');
    const cloudLabel = document.getElementById('cloud-chip-label');
    if (cloudChip) {
        cloudChip.classList.toggle('active', isCloud);
        if (cloudLabel) cloudLabel.textContent = isCloud ? 'Cloud' : 'Local';
    }
}

export async function loadConfig() {
    try {
        const r = await fetch(`${API}/config`);
        if (!r.ok) throw new Error('failed to load config');
        const cfg = await r.json();

        const modelEl = document.getElementById('cfg-model');
        if (modelEl) modelEl.value = cfg.model || 'gemma4:e2b';

        // Sync the ollama model chip label with the loaded value
        const ollamaModelLabel = document.getElementById('ollama-model-label');
        const ollamaOptions = document.querySelectorAll('#ollama-model-options .chip-option');
        if (ollamaModelLabel && ollamaOptions.length) {
            const currentModel = cfg.model || 'gemma4:e2b';
            let matched = false;
            ollamaOptions.forEach(opt => {
                if (opt.getAttribute('data-value') === currentModel) {
                    ollamaModelLabel.textContent = opt.textContent.trim();
                    opt.classList.add('active');
                    matched = true;
                } else {
                    opt.classList.remove('active');
                }
            });
            if (!matched) ollamaModelLabel.textContent = currentModel;
        }

        const maxSearchesEl = document.getElementById('cfg-max-searches');
        if (maxSearchesEl) maxSearchesEl.value = cfg.max_searches || 5;

        setInputDisplay('max-searches-val', cfg.max_searches || 5);

        const sysPromptEl = document.getElementById('cfg-system-prompt');
        if (cfg.system_prompt && sysPromptEl) sysPromptEl.value = cfg.system_prompt;

        setStatus(true, 'connected');

        const cloudModelEl = document.getElementById('cfg-cloud-model');
        if (cloudModelEl) cloudModelEl.value = cfg.cloud_model || cfg.groq_model || 'openai/gpt-oss-20b';

        // Always open in Local mode regardless of the backend's persisted global
        // provider. The user opens a fresh (empty) chat on load, and switching
        // to an old chat resolves that chat's own stored provider. Push the
        // Local default to the backend so a first message starts local.
        reflectProvider('ollama');
        if ((cfg.use_provider || 'ollama') !== 'ollama') {
            try {
                await fetch(`${API}/config`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ use_provider: 'ollama', use_external_provider: false }),
                });
            } catch { /* non-fatal — UI still shows Local */ }
        }

        const useCloudEl = document.getElementById('cfg-use-cloud');

        // Wire the cloud toggle to show/hide the thinking row live (no apply needed)
        if (useCloudEl && !useCloudEl._thinkingListenerBound) {
            useCloudEl._thinkingListenerBound = true;
            useCloudEl.addEventListener('change', () => {
                syncThinkingRowVisibility(useCloudEl.checked);
            });
        }
    } catch (err) {
        setStatus(false, 'server offline');
    }
}

export async function applyConfig() {
    const useCloud = document.getElementById('cfg-use-cloud').checked;
    const nextProvider = useCloud ? 'openrouter' : 'ollama';
    const providerChanged = nextProvider !== currentUseProvider;

    // Once a chat is active the provider is pinned to that conversation. Block
    // switching instead of wiping history — the user must start a new chat.
    if (providerChanged && providerLocked) {
        reflectProvider(currentUseProvider); // revert the toggle
        showToast('Provider is locked for this chat — start a new chat to switch');
        return;
    }

    const updates = {
        model: document.getElementById('cfg-model').value.trim(),
        max_searches: parseInt(document.getElementById('cfg-max-searches').value, 10),
        system_prompt: document.getElementById('cfg-system-prompt').value.trim(),
        use_provider: nextProvider,
        use_external_provider: useCloud,
        cloud_model: document.getElementById('cfg-cloud-model').value.trim(),
    };

    try {
        const r = await fetch(`${API}/config`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(updates),
        });
        if (!r.ok) throw new Error('failed to apply config');

        showToast('config applied');
        currentUseProvider = updates.use_provider;
    } catch (err) {
        showToast('failed to apply config');
    }
}


// ── Reindex buttons ───────────────────────────────────────────────────────────

export function bindReindexButtons() {
    const filesBtn = document.getElementById('btn-reindex-files');
    const ocrBtn = document.getElementById('btn-reindex-ocr');

    if (filesBtn) {
        filesBtn.addEventListener('click', async () => {
            if (!confirm('Re-index all workspace files? This runs in the background.')) return;
            try {
                const r = await fetch(`${API}/reindex/files`, { method: 'POST' });
                if (!r.ok) throw new Error();
                showToast('File re-index started');
            } catch {
                showToast('Failed to start file re-index');
            }
        });
    }

    if (ocrBtn) {
        ocrBtn.addEventListener('click', async () => {
            if (!confirm('Re-run OCR on all notes pages? This clears existing OCR output and re-processes everything. May take a few minutes.')) return;
            try {
                const r = await fetch(`${API}/reindex/ocr`, { method: 'POST' });
                if (!r.ok) throw new Error();
                showToast('OCR re-index started');
            } catch {
                showToast('Failed to start OCR re-index');
            }
        });
    }
}
