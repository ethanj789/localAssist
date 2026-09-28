import { API } from './constants.js';
import { showToast, setInputDisplay, scrollToBottom, escHtml } from './utils/dom.js';
import { initThemes } from './ui/theme.js';
import { loadConfig, applyConfig, bindReindexButtons, reflectProvider, setProviderLocked, providerLocked } from './api/config.js';
import {
    sendMessage,
    cancelStream,
    clearHistory,
    currentConversationId,
    setCurrentConversationId,
    getConversations,
    getConversationDetails,
    renameConversation,
    deleteConversation
} from './api/chat.js';
// import { initVoiceRecorder, bindVoiceButton } from './api/voice.js'; // DEAD CODE — replaced by Web Speech API
import { initWebSpeechVoice, bindWebSpeechButton } from './api/webSpeechVoice.js';
import { initMemoryUI } from './ui/memory.js';
import { initEditPanel, handleProposeEdit } from './ui/editPanel.js';
import { appendMsg, appendPill, renderMarkdown, renderMath } from './ui/chatRenderer.js';
import { initSkillsAutocomplete, loadSkills, isDropdownVisible } from './ui/skillsAutocomplete.js';

async function init() {
    initThemes();
    // initVoiceRecorder();  // DEAD CODE — Whisper-based voice disabled
    // bindVoiceButton();   // DEAD CODE — Whisper-based voice disabled
    initWebSpeechVoice();
    bindWebSpeechButton();
    bindInputEvents();
    bindButtons();
    initMemoryUI();
    initEditPanel();
    initCustomSelect();
    initAutoScroll();
    initSkillsAutocomplete();
    bindReindexButtons();
    bindSettingsAccordion();

    // Hook up custom events
    document.addEventListener('conversations-updated', loadConversations);
    document.addEventListener('conversation-id-updated', (e) => {
        // A message just created/continued a conversation — pin the provider so
        // it can't be switched mid-chat.
        setProviderLocked(true);
        loadConversations();
    });
    document.addEventListener('conversation-cleared', startNewChat);

    await loadConfig();
    await loadSkills();
    startStatusPolling();

    // Fetch and display existing conversations
    await loadConversations();

    // Start in a fresh, empty chat state instead of resuming the last conversation
    startNewChat(false);
}

function initAutoScroll() {
    const msgs = document.getElementById('messages');
    if (!msgs) return;

    const observer = new MutationObserver(() => {
        scrollToBottom(msgs);
    });

    observer.observe(msgs, { childList: true, subtree: true, characterData: true });
}

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
    const stopBtn = document.getElementById('stop-btn');

    if (sendBtn) {
        sendBtn.addEventListener('click', sendMessage);
    }

    if (stopBtn) {
        stopBtn.addEventListener('click', () => {
            cancelStream();
        });
    }

    // Toggle send/stop button visibility based on streaming state
    const observer = new MutationObserver(() => {
        // sendBtn is disabled during streaming
        const streaming = sendBtn && sendBtn.disabled;
        if (sendBtn) sendBtn.classList.toggle('hidden', streaming);
        if (stopBtn) stopBtn.classList.toggle('hidden', !streaming);
    });

    if (sendBtn) {
        observer.observe(sendBtn, { attributes: true, attributeFilter: ['disabled'] });
    }
}

function bindButtons() {
    const applyBtn = document.querySelector('.btn-apply');
    if (applyBtn) applyBtn.addEventListener('click', applyConfig);

    const clearBtn = document.querySelector('.btn-clear');
    if (clearBtn) clearBtn.addEventListener('click', clearHistory);

    const newChatBtn = document.getElementById('new-chat-btn');
    if (newChatBtn) newChatBtn.addEventListener('click', startNewChat);

    // Ollama think toggle — now lives as a sidebar checkbox (#cfg-ollama-thinking)
    // Visibility is managed by config.js syncThinkingRowVisibility()

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
                if (isDropdownVisible()) return; // let the dropdown handle it
                e.preventDefault();
                sendMessage();
            }
        });
    }
}

function initCustomSelect() {
    const wrapper = document.getElementById('model-select-wrapper');
    const trigger = document.getElementById('model-select-trigger');
    const options = wrapper ? wrapper.querySelectorAll('.chip-option') : [];
    const hiddenInput = document.getElementById('model-selector');

    if (!wrapper || !trigger) return;

    trigger.addEventListener('click', (e) => {
        e.stopPropagation();
        wrapper.classList.toggle('open');
    });

    options.forEach(opt => {
        opt.addEventListener('click', () => {
            const val = opt.getAttribute('data-value');
            const icon = opt.querySelector('.chip-opt-icon')?.textContent || '⚡';
            const label = opt.textContent.trim();

            hiddenInput.value = val;
            trigger.innerHTML = `<span class="chip-icon">${icon}</span> ${label}`;

            options.forEach(o => o.classList.remove('active'));
            opt.classList.add('active');

            wrapper.classList.remove('open');
        });
    });

    document.addEventListener('click', () => {
        wrapper.classList.remove('open');
    });

    // ── Ollama model chip-select ──
    const ollamaWrapper = document.getElementById('ollama-model-select-wrapper');
    const ollamaTrigger = document.getElementById('ollama-model-trigger');
    const ollamaOptions = ollamaWrapper ? ollamaWrapper.querySelectorAll('.chip-option') : [];
    const ollamaHidden = document.getElementById('cfg-model');

    if (ollamaWrapper && ollamaTrigger) {
        ollamaTrigger.addEventListener('click', (e) => {
            e.stopPropagation();
            ollamaWrapper.classList.toggle('open');
        });

        ollamaOptions.forEach(opt => {
            opt.addEventListener('click', () => {
                const val = opt.getAttribute('data-value');
                const label = opt.textContent.trim();

                ollamaHidden.value = val;
                document.getElementById('ollama-model-label').textContent = label;

                ollamaOptions.forEach(o => o.classList.remove('active'));
                opt.classList.add('active');

                ollamaWrapper.classList.remove('open');

                // Auto-apply the model change
                applyConfig();
            });
        });

        document.addEventListener('click', () => {
            ollamaWrapper.classList.remove('open');
        });
    }

    // Cloud chip toggle
    const cloudChip = document.getElementById('chip-cloud-toggle');
    const cloudLabel = document.getElementById('cloud-chip-label');
    const thinkingChip = document.getElementById('chip-thinking-toggle');
    const ollamaModelChip = document.getElementById('ollama-model-select-wrapper');
    const modeSelector = document.getElementById('model-select-wrapper');

    if (cloudChip) {
        cloudChip.addEventListener('click', () => {
            if (providerLocked) {
                showToast('Provider is locked for this chat — start a new chat to switch');
                return;
            }
            const cfgCloud = document.getElementById('cfg-use-cloud');
            const isNowCloud = !cfgCloud.checked;
            cfgCloud.checked = isNowCloud;
            cloudChip.classList.toggle('active', isNowCloud);
            cloudLabel.textContent = isNowCloud ? 'Cloud' : 'Local';
            // Show/hide thinking chip and ollama model selector (only for local)
            if (thinkingChip) thinkingChip.style.display = isNowCloud ? 'none' : 'inline-flex';
            if (ollamaModelChip) ollamaModelChip.style.display = isNowCloud ? 'none' : '';
            // Show/hide mode selector (only for cloud)
            if (modeSelector) modeSelector.style.display = isNowCloud ? '' : 'none';
            // Auto-apply provider change
            applyConfig();
        });
    }

    if (thinkingChip) {
        thinkingChip.addEventListener('click', () => {
            const cfgThinking = document.getElementById('cfg-ollama-thinking');
            cfgThinking.checked = !cfgThinking.checked;
            thinkingChip.classList.toggle('active', cfgThinking.checked);
        });
    }
}

// ====== CONVERSATION MANAGEMENT ======

async function loadConversations() {
    const listEl = document.getElementById('conversation-list');
    if (!listEl) return;

    try {
        const conversations = await getConversations();
        listEl.innerHTML = '';

        if (conversations.length === 0) {
            listEl.innerHTML = '<div style="color:var(--muted); font-size:12px; padding: 8px 10px; font-style:italic;">No past chats</div>';
            return;
        }

        conversations.forEach(conv => {
            const item = document.createElement('div');
            item.className = 'conversation-item';
            if (conv.id === currentConversationId) {
                item.classList.add('active');
            }
            item.dataset.id = conv.id;

            const textContainer = document.createElement('div');
            textContainer.className = 'conversation-text-container';

            const titleEl = document.createElement('div');
            titleEl.className = 'conversation-title';
            titleEl.textContent = conv.title || 'Untitled Chat';
            textContainer.appendChild(titleEl);
            item.appendChild(textContainer);

            // Actions
            const actions = document.createElement('div');
            actions.className = 'conversation-actions';

            // Rename Button
            const renameBtn = document.createElement('button');
            renameBtn.className = 'conversation-btn btn-rename';
            renameBtn.title = 'Rename';
            renameBtn.innerHTML = '✏️';
            renameBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                startRename(item, conv.id, titleEl);
            });
            actions.appendChild(renameBtn);

            // Delete Button
            const deleteBtn = document.createElement('button');
            deleteBtn.className = 'conversation-btn btn-delete';
            deleteBtn.title = 'Delete';
            deleteBtn.innerHTML = '🗑️';
            deleteBtn.addEventListener('click', async (e) => {
                e.stopPropagation();
                if (confirm('Delete this conversation?')) {
                    try {
                        await deleteConversation(conv.id);
                        showToast('Conversation deleted');
                    } catch (err) {
                        showToast('Error deleting conversation');
                    }
                }
            });
            actions.appendChild(deleteBtn);

            item.appendChild(actions);

            // Click to resume
            item.addEventListener('click', () => {
                selectConversation(conv.id);
            });

            listEl.appendChild(item);
        });
    } catch (e) {
        console.warn('Failed to load conversations:', e);
    }
}

function startRename(itemEl, id, titleEl) {
    if (itemEl.querySelector('.conversation-title-input')) return;

    const oldTitle = titleEl.textContent;
    titleEl.style.display = 'none';

    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'conversation-title-input';
    input.value = oldTitle;

    const container = titleEl.parentNode;
    container.appendChild(input);
    input.focus();
    input.select();

    async function commitRename() {
        const newTitle = input.value.trim();
        if (newTitle && newTitle !== oldTitle) {
            try {
                await renameConversation(id, newTitle);
                showToast('Renamed conversation');
            } catch (err) {
                showToast('Error renaming conversation');
                titleEl.style.display = 'block';
            }
        } else {
            titleEl.style.display = 'block';
        }
        input.remove();
    }

    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            commitRename();
        } else if (e.key === 'Escape') {
            titleEl.style.display = 'block';
            input.remove();
        }
    });

    input.addEventListener('blur', () => {
        setTimeout(commitRename, 200);
    });
}

async function selectConversation(id) {
    if (id === currentConversationId) return;

    try {
        const details = await getConversationDetails(id);
        setCurrentConversationId(id);

        // Pin the UI to whichever provider this conversation was created with,
        // and lock switching. The backend already routes this conversation to
        // its stored provider regardless of the global toggle.
        const convProvider = details.conversation?.provider || 'ollama';
        reflectProvider(convProvider);
        setProviderLocked(true);

        renderConversationHistory(details.messages);

        document.querySelectorAll('.conversation-item').forEach(item => {
            if (item.dataset.id === id) {
                item.classList.add('active');
            } else {
                item.classList.remove('active');
            }
        });

        showToast('Resumed conversation');
    } catch (err) {
        showToast('Error loading conversation');
        console.error(err);
    }
}

function renderConversationHistory(messages) {
    const msgs = document.getElementById('messages');
    msgs.innerHTML = '';

    let lastAssistantDiv = null;

    messages.forEach(msg => {
        if (msg.role === 'user') {
            appendMsg('user', escHtml(msg.content));
            lastAssistantDiv = null;
        } else if (msg.role === 'assistant') {
            const div = appendMsg('assistant', '');
            lastAssistantDiv = div;
            const body = div.querySelector('.msg-body');
            const flow = div.querySelector('.msg-flow');

            // Render thinking block if present — into the flow container
            if (msg.thinking_content) {
                const details = document.createElement('details');
                details.className = 'thinking-block';

                const summary = document.createElement('summary');
                summary.className = 'thinking-summary';
                summary.textContent = '💭 thoughts';
                details.appendChild(summary);

                const content = document.createElement('div');
                content.className = 'thinking-content md-content';
                renderMarkdown(content, msg.thinking_content);
                renderMath(content);
                details.appendChild(content);

                flow.appendChild(details);
            }

            // Render tool calls as pills in the flow container
            if (msg.tool_calls && msg.tool_calls.length > 0) {
                msg.tool_calls.forEach(tc => {
                    const fn = tc.function || tc;
                    const name = fn.name;
                    let args = {};
                    try {
                        args = typeof fn.arguments === 'string' ? JSON.parse(fn.arguments) : (fn.arguments || {});
                    } catch (e) {
                        args = {};
                    }
                    let label = `tool requested: ${name}`;
                    if (name === 'web_search') {
                        label = `tool requested: web_search "${args?.query || ''}"`;
                    } else if (name === 'fetch_webpage') {
                        label = `tool requested: fetch_webpage ${args?.url || ''}`;
                    } else if (name === 'list_files') {
                        label = `tool requested: list_files ${args?.subdir || '/'}`;
                    } else if (name === 'read_file') {
                        label = `tool requested: read_file ${args?.path || ''}`;
                    }
                    appendPill(div, label, 'done');
                });
            }

            if (msg.content) {
                const mdDiv = document.createElement('div');
                mdDiv.className = 'md-content';
                body.appendChild(mdDiv);
                renderMarkdown(mdDiv, msg.content);
                renderMath(mdDiv);
            }
        } else if (msg.role === 'tool') {
            if (lastAssistantDiv) {
                if (msg.content === 'APPROVED' || msg.content?.startsWith('REJECTED')) {
                    appendPill(lastAssistantDiv, `edit proposed — ${msg.content}`, 'done');
                }
            }
        }
    });

    scrollToBottom(msgs);
}

function startNewChat(showToast = true) {
    setCurrentConversationId(null);
    const messagesEl = document.getElementById('messages');
    if (messagesEl) messagesEl.innerHTML = '';

    const userInput = document.getElementById('user-input');
    if (userInput) {
        userInput.value = '';
        userInput.style.height = 'auto';
    }

    document.querySelectorAll('.conversation-item').forEach(item => {
        item.classList.remove('active');
    });

    // Fresh chat: unlock provider switching and reset to Local default.
    setProviderLocked(false);
    reflectProvider('ollama');

    if (showToast) {
        showToast('Started new conversation');
    }
}

// ====== SETTINGS ACCORDION ======

function bindSettingsAccordion() {
    const toggleBtn = document.getElementById('settings-toggle-btn');
    const content = document.getElementById('settings-content');
    const arrow = document.getElementById('settings-arrow');

    if (toggleBtn && content) {
        toggleBtn.addEventListener('click', () => {
            const isHidden = content.style.display === 'none';
            if (isHidden) {
                content.style.display = 'flex';
                toggleBtn.classList.add('open');
                arrow.textContent = '▾';
            } else {
                content.style.display = 'none';
                toggleBtn.classList.remove('open');
                arrow.textContent = '▸';
            }
        });
    }
}

// Start application
init();
