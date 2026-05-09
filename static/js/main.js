import { API } from './constants.js';
import { showToast, setInputDisplay, scrollToBottom } from './utils/dom.js';
import { initThemes } from './ui/theme.js';
import { loadConfig, applyConfig } from './api/config.js';
import { sendMessage, clearHistory } from './api/chat.js';
import { initVoiceRecorder, bindVoiceButton } from './api/voice.js';
import { initMemoryUI } from './ui/memory.js';

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
    initMemoryUI();
    initCustomSelect();
    initAutoScroll();
    await loadConfig();
    startStatusPolling();
}

function initAutoScroll() {
    const msgs = document.getElementById('messages');
    if (!msgs) return;

    const observer = new MutationObserver(() => {
        scrollToBottom(msgs);
    });

    observer.observe(msgs, { childList: true, subtree: true, characterData: true });
}


// function startStatusPolling() {
//     const statusDiv = document.getElementById('embedding-status');
//     setInterval(async () => {
//         try {
//             const res = await fetch(`${API}/status`);
//             if (res.ok) {
//                 const data = await res.json();
//                 statusDiv.style.display = data.busy ? 'flex' : 'none';
//             }
//         } catch (e) {
//             console.warn('Status poll failed:', e);
//         }
//     }, 2000);
// }
function startStatusPolling() {
    const statusDiv = document.getElementById('embedding-status');
    const evtSource = new EventSource(`${API}/status/stream`);
    let lastBusy = false;

    evtSource.onmessage = (e) => {
        const payload = JSON.parse(e.data);
        if (payload.event === 'status') {
            const isBusy = payload.data === true;
            statusDiv.style.display = isBusy ? 'flex' : 'none';

            if (isBusy && !lastBusy) {
                showToast('Updating embeddings...');
            } else if (!isBusy && lastBusy) {
                showToast('Embeddings updated');
            }
            lastBusy = isBusy;
        }
    };

    evtSource.onerror = () => {
        console.warn('SSE connection lost');
        evtSource.close();
    };
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

    const menuToggle = document.querySelector('.menu-toggle');
    const aside = document.querySelector('aside');
    const overlay = document.getElementById('sidebar-overlay');

    if (menuToggle && aside && overlay) {
        menuToggle.addEventListener('click', () => {
            aside.classList.toggle('open');
            overlay.classList.toggle('active');
        });

        overlay.addEventListener('click', () => {
            aside.classList.remove('open');
            overlay.classList.remove('active');
        });
    }

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

function initCustomSelect() {
    const wrapper = document.getElementById('model-select-wrapper');
    const trigger = document.getElementById('model-select-trigger');
    const options = document.querySelectorAll('.select-option');
    const hiddenInput = document.getElementById('model-selector');

    if (!wrapper || !trigger) return;

    trigger.addEventListener('click', (e) => {
        e.stopPropagation();
        wrapper.classList.toggle('open');
    });

    options.forEach(opt => {
        opt.addEventListener('click', () => {
            const val = opt.getAttribute('data-value');
            const label = opt.textContent;

            hiddenInput.value = val;
            trigger.textContent = label;

            options.forEach(o => o.classList.remove('active'));
            opt.classList.add('active');

            wrapper.classList.remove('open');
        });
    });

    document.addEventListener('click', () => {
        wrapper.classList.remove('open');
    });
}

// Start application
init();
