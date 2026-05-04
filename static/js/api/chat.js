import { API } from '../constants.js';
import { escHtml, setStatus, showToast } from '../utils/dom.js';
import { appendMsg, appendPill, renderLinkCard, renderMarkdown } from '../ui/chatRenderer.js';

export let isStreaming = false;

export function setIsStreaming(value) {
    isStreaming = value;
}

export async function clearHistory() {
    await fetch(`${API}/history`, { method: 'DELETE' });
    const messagesEl = document.getElementById('messages');
    if (messagesEl) messagesEl.innerHTML = '';
    showToast('history cleared');
}

export async function sendMessage() {
    if (isStreaming) return;
    const input = document.getElementById('user-input');
    const text = input.value.trim();
    if (!text) return;

    input.value = '';
    input.style.height = 'auto';
    setIsStreaming(true);
    document.getElementById('send-btn').disabled = true;
    setStatus(true, 'thinking...');

    appendMsg('user', escHtml(text));

    const aId = 'msg-' + Date.now();
    const aDiv = appendMsg('assistant', '', aId);
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
    };

    try {
        const resp = await fetch(`${API}/chat`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ message: text }),
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
        aBody.innerHTML = `<span style="color:var(--warn)">error: ${e.message}</span>`;
        setStatus(false, 'error');
    } finally {
        cursor.remove();
        setIsStreaming(false);
        document.getElementById('send-btn').disabled = false;
        setStatus(true, 'ready');
    }
}

export function handleStreamEvent(event, data, state) {
    const { aDiv, mdDiv } = state;

    if (event === 'token') {
        state.fullText += data;
        renderMarkdown(mdDiv, state.fullText);
        const msgs = document.getElementById('messages');
        if (msgs) msgs.scrollTop = 9999;
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

        if (state.linkCards.length > 0) {
            const tray = document.createElement('div');
            tray.className = 'link-card-tray';
            state.linkCards.forEach(card => renderLinkCard(tray, card));
            aBody.appendChild(tray);
        }
    }
}
