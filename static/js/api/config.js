import { API } from '../constants.js';
import { setStatus, showToast, setInputDisplay } from '../utils/dom.js';

export let currentUseGroq = false;

export async function loadConfig() {
    try {
        const r = await fetch(`${API}/config`);
        if (!r.ok) throw new Error('failed to load config');
        const cfg = await r.json();
        
        const modelEl = document.getElementById('cfg-model');
        if (modelEl) modelEl.value = cfg.model || 'gemma4:2b';
        
        const maxSearchesEl = document.getElementById('cfg-max-searches');
        if (maxSearchesEl) maxSearchesEl.value = cfg.max_searches || 5;
        
        setInputDisplay('max-searches-val', cfg.max_searches || 5);
        
        const sysPromptEl = document.getElementById('cfg-system-prompt');
        if (cfg.system_prompt && sysPromptEl) sysPromptEl.value = cfg.system_prompt;
        
        setStatus(true, 'connected');
        
        const groqModelEl = document.getElementById('cfg-groq-model');
        if (cfg.groq_model && groqModelEl) groqModelEl.value = cfg.groq_model;
        
        const useGroqEl = document.getElementById('cfg-use-groq');
        if (useGroqEl) useGroqEl.checked = cfg.use_groq || false;
        
        currentUseGroq = cfg.use_groq || false;
    } catch (err) {
        setStatus(false, 'server offline');
    }
}

export async function applyConfig() {
    const updates = {
        model: document.getElementById('cfg-model').value.trim(),
        max_searches: parseInt(document.getElementById('cfg-max-searches').value, 10),
        system_prompt: document.getElementById('cfg-system-prompt').value.trim(),
        use_groq: document.getElementById('cfg-use-groq').checked,
        groq_model: document.getElementById('cfg-groq-model').value.trim(),
    };

    const groqChanged = updates.use_groq !== currentUseGroq;

    try {
        const r = await fetch(`${API}/config`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(updates),
        });
        if (!r.ok) throw new Error('failed to apply config');

        if (groqChanged) {
            await fetch(`${API}/history`, { method: 'DELETE' });
            document.getElementById('messages').innerHTML = '';
            showToast('config applied — history cleared (backend switched)');
        } else {
            showToast('config applied');
        }
        currentUseGroq = updates.use_groq;
    } catch (err) {
        showToast('failed to apply config');
    }
}
