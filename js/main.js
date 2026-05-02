import { API } from './constants.js';
import { showToast, setInputDisplay } from './utils/dom.js';
import { initThemes } from './ui/theme.js';
import { loadConfig, applyConfig } from './api/config.js';
import { sendMessage, clearHistory } from './api/chat.js';
import { initVoiceRecorder, bindVoiceButton } from './api/voice.js';

async function init() {
    try {
        if (!window.__historyCleared) {
            window.__historyCleared = true;
            const res = await fetch(`${API}/history`, { method: 'DELETE' });
            if (res.ok) {
                showToast('history cleared');
            } else {
                console.warn('Failed to clear history:', res.status);
            }
        }
    } catch (e) {
        console.warn('Error clearing history:', e);
    }
    
    initThemes();
    initVoiceRecorder();
    bindVoiceButton();
    bindInputEvents();
    bindButtons();
    await loadConfig();
}

function bindInputEvents() {
    const maxSearches = document.getElementById('cfg-max-searches');
    if (maxSearches) {
        maxSearches.addEventListener('input', e => {
            setInputDisplay('max-searches-val', e.target.value);
        });
    }

    const sendBtn = document.getElementById('send-btn');
    if (sendBtn) {
        sendBtn.addEventListener('click', sendMessage);
    }
}

function bindButtons() {
    const applyBtn = document.querySelector('.btn-apply');
    if (applyBtn) applyBtn.addEventListener('click', applyConfig);
    
    const clearBtn = document.querySelector('.btn-clear');
    if (clearBtn) clearBtn.addEventListener('click', clearHistory);
    
    const userInput = document.getElementById('user-input');
    if (userInput) {
        userInput.addEventListener('input', function () {
            this.style.height = 'auto';
            this.style.height = Math.min(this.scrollHeight, 140) + 'px';
        });
        userInput.addEventListener('keydown', e => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendMessage();
            }
        });
    }
}

// Start application
init();
