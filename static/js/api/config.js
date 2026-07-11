import { API } from '../constants.js';
import { setStatus, showToast, setInputDisplay } from '../utils/dom.js';

export let currentUseProvider = 'ollama';

function syncThinkingRowVisibility(isCloud) {
    const row = document.getElementById('ollama-thinking-row');
    if (row) row.style.display = isCloud ? 'none' : 'flex';
}

export async function loadConfig() {
    try {
        const r = await fetch(`${API}/config`);
        if (!r.ok) throw new Error('failed to load config');
        const cfg = await r.json();

        const modelEl = document.getElementById('cfg-model');
        if (modelEl) modelEl.value = cfg.model || 'gemma4:e2b';

        const maxSearchesEl = document.getElementById('cfg-max-searches');
        if (maxSearchesEl) maxSearchesEl.value = cfg.max_searches || 5;

        setInputDisplay('max-searches-val', cfg.max_searches || 5);

        const sysPromptEl = document.getElementById('cfg-system-prompt');
        if (cfg.system_prompt && sysPromptEl) sysPromptEl.value = cfg.system_prompt;

        setStatus(true, 'connected');

        const cloudModelEl = document.getElementById('cfg-cloud-model');
        if (cloudModelEl) cloudModelEl.value = cfg.cloud_model || cfg.groq_model || 'openai/gpt-oss-20b';

        const isCloud = (cfg.use_provider || (cfg.use_external_provider ? 'openrouter' : 'ollama')) !== 'ollama';
        const useCloudEl = document.getElementById('cfg-use-cloud');
        if (useCloudEl) useCloudEl.checked = isCloud;

        currentUseProvider = cfg.use_provider || (cfg.use_external_provider ? 'openrouter' : 'ollama');

        syncThinkingRowVisibility(isCloud);

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
    const updates = {
        model: document.getElementById('cfg-model').value.trim(),
        max_searches: parseInt(document.getElementById('cfg-max-searches').value, 10),
        system_prompt: document.getElementById('cfg-system-prompt').value.trim(),
        use_provider: useCloud ? 'openrouter' : 'ollama',
        use_external_provider: useCloud,
        cloud_model: document.getElementById('cfg-cloud-model').value.trim(),
    };

    const providerChanged = updates.use_provider !== currentUseProvider;

    try {
        const r = await fetch(`${API}/config`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(updates),
        });
        if (!r.ok) throw new Error('failed to apply config');

        if (providerChanged) {
            await fetch(`${API}/history`, { method: 'DELETE' });
            document.getElementById('messages').innerHTML = '';
            showToast('config applied — history cleared (backend switched)');
        } else {
            showToast('config applied');
        }
        currentUseProvider = updates.use_provider;
    } catch (err) {
        showToast('failed to apply config');
    }
}
