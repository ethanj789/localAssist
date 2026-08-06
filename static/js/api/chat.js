import { API } from '../constants.js';
import { escHtml, setStatus, showToast, scrollToBottom } from '../utils/dom.js';
import { appendMsg, appendPill, renderLinkCard, renderMarkdown, renderActionButton, ensureThinkingBlock, appendOllamaStats } from '../ui/chatRenderer.js';
import { handleProposeEdit } from '../ui/editPanel.js';

export let isStreaming = false;
export let currentConversationId = null;
let currentAbortController = null;

export function setIsStreaming(value) {
    isStreaming = value;
}

export function setCurrentConversationId(value) {
    currentConversationId = value;
}

export function cancelStream() {
    if (currentAbortController) {
        currentAbortController.abort();
        currentAbortController = null;
    }
}

export async function clearHistory() {
    await fetch(`${API}/history`, { method: 'DELETE' });
    currentConversationId = null;
    const messagesEl = document.getElementById('messages');
    if (messagesEl) messagesEl.innerHTML = '';
    document.dispatchEvent(new CustomEvent('conversations-updated'));
    document.dispatchEvent(new CustomEvent('conversation-cleared'));
    showToast('All conversations cleared');
}

export async function getConversations() {
    const res = await fetch(`${API}/conversations`);
    if (!res.ok) throw new Error('Failed to fetch conversations');
    return await res.json();
}

export async function getConversationDetails(id) {
    const res = await fetch(`${API}/conversations/${id}`);
    if (!res.ok) throw new Error('Failed to fetch conversation details');
    return await res.json();
}

export async function renameConversation(id, title) {
    const res = await fetch(`${API}/conversations/${id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title }),
    });
    if (!res.ok) throw new Error('Failed to rename conversation');
    document.dispatchEvent(new CustomEvent('conversations-updated'));
    return await res.json();
}

export async function deleteConversation(id) {
    const res = await fetch(`${API}/conversations/${id}`, {
        method: 'DELETE',
    });
    if (!res.ok) throw new Error('Failed to delete conversation');
    if (currentConversationId === id) {
        currentConversationId = null;
        document.dispatchEvent(new CustomEvent('conversation-cleared'));
    }
    document.dispatchEvent(new CustomEvent('conversations-updated'));
    return await res.json();
}


export async function sendMessage(customText = null) {
    if (isStreaming) return;

    let text = '';
    if (customText && typeof customText === 'string') {
        text = customText.trim();
    } else {
        const input = document.getElementById('user-input');
        if (!input) return;
        text = input.value.trim();
        input.value = '';
        input.style.height = 'auto';
    }
    if (!text) return;

    setIsStreaming(true);
    document.getElementById('send-btn').disabled = true;
    setStatus(true, 'thinking...');

    appendMsg('user', escHtml(text));
    scrollToBottom(document.getElementById('messages'), true);

    const aId = 'msg-' + Date.now();
    const aDiv = appendMsg('assistant', '', aId);
    scrollToBottom(document.getElementById('messages'), true);

    const aBody = aDiv.querySelector('.msg-body');
    const mdDiv = document.createElement('div');
    mdDiv.className = 'md-content';
    let cursor = document.createElement('span');
    cursor.className = 'cursor';
    aBody.appendChild(mdDiv);
    aBody.appendChild(cursor);

    const state = {
        aDiv,
        mdDiv,
        fullText: '',
        currentPill: null,
        cursor,
        linkCards: [],
        actionButtons: [],
        thinkingBlock: null,
        ollamaStats: null,
    };

    const modelSelector = document.getElementById('model-selector');
    const model = modelSelector ? modelSelector.value : 'default';

    const thinkEl = document.getElementById('cfg-ollama-thinking');
    const ollamaThinking = thinkEl ? thinkEl.checked : false;

    currentAbortController = new AbortController();
    const signal = currentAbortController.signal;

    try {
        const resp = await fetch(`${API}/chat`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                message: text,
                model: model,
                conversation_id: currentConversationId,
                ollama_thinking: ollamaThinking,
            }),
            signal,
        });

        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buf = '';

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buf += decoder.decode(value, { stream: true });

            const lines = buf.split('\n\n');
            buf = lines.pop();

            for (const line of lines) {
                if (!line.startsWith('data: ')) continue;
                let parsed;
                try { parsed = JSON.parse(line.slice(6)); } catch { continue; }

                const { event, data } = parsed;
                handleStreamEvent(event, data, state);
            }
        }
    } catch (e) {
        if (e.name === 'AbortError') {
            // User cancelled — show a subtle indicator
            const meta = document.createElement('div');
            meta.className = 'meta';
            meta.textContent = '⏹ stopped';
            aBody.appendChild(meta);
            setStatus(true, 'stopped');
        } else {
            aBody.innerHTML = `<span style="color:var(--warn)">error: ${e.message}</span>`;
            setStatus(false, 'error');
        }
    } finally {
        currentAbortController = null;
        cursor.remove();
        setIsStreaming(false);
        document.getElementById('send-btn').disabled = false;
        setStatus(true, 'ready');
    }
}

export function handleStreamEvent(event, data, state) {
    const { aDiv, mdDiv } = state;

    if (event === 'conversation_id') {
        setCurrentConversationId(data);
        document.dispatchEvent(new CustomEvent('conversation-id-updated', { detail: data }));
    }

    if (event === 'token') {
        state.fullText += data;
        renderMarkdown(mdDiv, state.fullText);
        // Collapse the thinking block once when response starts, then leave it alone
        if (state.thinkingBlock && !state._thinkingCollapsed) {
            state._thinkingCollapsed = true;
            state.thinkingBlock.open = false;
            const summary = state.thinkingBlock.querySelector('.thinking-summary');
            if (summary) summary.textContent = '💭 thoughts';
        }
    }

    if (event === 'clear_tokens') {
        // The model emitted some content tokens then decided to call a tool instead.
        // Wipe the streamed text so we don't show stray pre-tool content.
        state.fullText = '';
        mdDiv.innerHTML = '';
    }

    if (event === 'thinking_token') {
        const aBody = aDiv.querySelector('.msg-body');
        if (!state.thinkingBlock) {
            state.thinkingBlock = ensureThinkingBlock(aBody);
            state.thinkingBlock.open = true;
        }
        state.thinkingBlock.contentEl.textContent += data;
        // Keep the thinking block scrolled to bottom while streaming
        state.thinkingBlock.contentEl.scrollTop = state.thinkingBlock.contentEl.scrollHeight;
    }

    if (event === 'ollama_stats') {
        state.ollamaStats = JSON.parse(data);
    }



    if (event === 'tool_requested') {
        const info = JSON.parse(data);
        let label;
        if (info.tool === 'web_search') {
            label = `tool requested: web_search "${info.args?.query || ''}"`;
        } else if (info.tool === 'fetch_webpage') {
            label = `tool requested: fetch_webpage ${info.args?.url || ''}`;
        } else if (info.tool === 'list_files') {
            label = `tool requested: list_files ${info.args?.subdir || '/'}`;
        } else if (info.tool === 'read_file') {
            label = `tool requested: read_file ${info.args?.path || ''}`;
        } else if (info.tool === 'read_code_skeleton') {
            label = `tool requested: read_code_skeleton for file: ${info.args?.path || ''}`;
        } else if (info.tool === 'search_semantic') {
            label = `tool requested: conceptual search: "${info.args?.query || ''}"`;
        } else {
            label = `tool requested: ${info.tool}`;
        }
        appendPill(aDiv, label, 'done');
    }

    if (event === 'searching') {
        const info = JSON.parse(data);
        const label = info.label || (info.tool === 'web_search'
            ? `searching: "${info.args?.query}" (${info.count}/${info.max})`
            : `fetching: ${info.args?.url?.slice(0, 40)}...`);
        state.currentPill = appendPill(aDiv, label, 'active');
    }

    if (event === 'search_result') {
        if (state.currentPill) {
            state.currentPill.className = 'search-pill done';
            state.currentPill.innerHTML = `✓ ${state.currentPill.textContent.trim()}`;
        }
    }

    if (event === 'status') {
        appendPill(aDiv, data, 'done');
    }

    if (event === 'model') {
        const info = JSON.parse(data);
        appendPill(aDiv, `model: ${info.provider}/${info.model}`, 'done');
    }

    if (event === 'link_card') {
        state.linkCards.push(JSON.parse(data));
    }

    if (event === 'action_button') {
        console.log(data)
        state.actionButtons.push(JSON.parse(data));
    }

    if (event === 'propose_edit') {
        const editData = JSON.parse(data);
        handleProposeEdit(editData);
    }

    if (event === 'plan_update') {
        const { plan } = JSON.parse(data);
        const aBody = aDiv.querySelector('.msg-body');
        // Remove any existing plan tracker in this message
        const existing = aBody.querySelector('.plan-tracker');
        if (existing) existing.remove();

        const tracker = document.createElement('div');
        tracker.className = 'plan-tracker';

        const header = document.createElement('div');
        header.className = 'plan-tracker-header';
        header.textContent = `📋 ${plan.goal}`;
        tracker.appendChild(header);

        plan.steps.forEach((step, i) => {
            const stepEl = document.createElement('div');
            let cls = 'pending';
            if (step.status === 'done') cls = 'done';
            else if (step.status === 'failed') cls = 'failed';
            else if (i === plan.current_step) cls = 'current';
            stepEl.className = `plan-tracker-step ${cls}`;
            stepEl.innerHTML = `<span class="plan-step-icon"></span> <span>${step.description}</span>`;
            tracker.appendChild(stepEl);
        });

        aBody.appendChild(tracker);
    }

    if (event === 'skill_start') {
        const info = JSON.parse(data);
        const aBody = aDiv.querySelector('.msg-body');
        const progressEl = document.createElement('div');
        progressEl.className = 'skill-progress';
        progressEl.id = 'skill-progress-active';

        const header = document.createElement('div');
        header.className = 'skill-progress-header';
        header.textContent = `⚡ ${info.name}`;
        progressEl.appendChild(header);

        // Pre-render step placeholders
        for (let i = 0; i < info.total_steps; i++) {
            const stepEl = document.createElement('div');
            stepEl.className = 'skill-progress-step pending';
            stepEl.dataset.index = i;
            stepEl.innerHTML = `<span class="step-icon"></span> <span class="step-label">step ${i + 1}</span>`;
            progressEl.appendChild(stepEl);
        }

        aBody.appendChild(progressEl);
    }

    if (event === 'skill_step') {
        const info = JSON.parse(data);
        const progressEl = document.getElementById('skill-progress-active');
        if (progressEl) {
            const stepEl = progressEl.querySelector(`[data-index="${info.step_index}"]`);
            if (stepEl) {
                stepEl.className = `skill-progress-step ${info.status}`;
                const label = stepEl.querySelector('.step-label');
                if (label) {
                    if (info.status === 'running') {
                        label.textContent = `${info.step_id} — running...`;
                    } else if (info.status === 'thinking') {
                        label.textContent = `${info.step_id} — thinking...`;
                    } else if (info.status === 'done') {
                        label.textContent = `${info.step_id} — done`;
                    }
                }
            }
        }
    }

    if (event === 'skill_complete') {
        const progressEl = document.getElementById('skill-progress-active');
        if (progressEl) {
            progressEl.removeAttribute('id');
            const header = progressEl.querySelector('.skill-progress-header');
            if (header) {
                const info = JSON.parse(data);
                header.textContent = `✓ ${info.name} — ${info.steps_completed} steps completed`;
            }
        }
    }

    if (event === 'done') {
        const info = JSON.parse(data);
        if (state.cursor && state.cursor.parentNode) state.cursor.remove();
        const aBody = aDiv.querySelector('.msg-body');

        const stats = [];
        if (info.searches_used > 0) stats.push(`${info.searches_used} search${info.searches_used !== 1 ? 'es' : ''}`);
        if (info.emails_used > 0) stats.push(`${info.emails_used} email${info.emails_used !== 1 ? 's' : ''}`);
        if (info.tools_used > 0) stats.push(`${info.tools_used} total tool call${info.tools_used !== 1 ? 's' : ''}`);

        if (stats.length > 0) {
            const meta = document.createElement('div');
            meta.className = 'meta';
            meta.textContent = stats.join(', ') + ' used';
            aBody.appendChild(meta);
        }

        if (state.ollamaStats) {
            appendOllamaStats(aBody, state.ollamaStats);
        }

        if (state.linkCards.length > 0) {
            const tray = document.createElement('div');
            tray.className = 'link-card-tray';
            state.linkCards.forEach(card => renderLinkCard(tray, card));
            aBody.appendChild(tray);
        }

        if (state.actionButtons.length > 0) {
            const tray = document.createElement('div');
            tray.className = 'action-button-tray';
            state.actionButtons.forEach(action => renderActionButton(tray, action));
            aBody.appendChild(tray);
        }

        document.dispatchEvent(new CustomEvent('conversations-updated'));
    }
}
