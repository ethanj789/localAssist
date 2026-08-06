import { escHtml } from '../utils/dom.js';

export function appendMsg(role, content, id) {
    const msgs = document.getElementById('messages');
    const div = document.createElement('div');
    div.className = `msg ${role}`;
    if (id) div.id = id;
    div.innerHTML = `
        <div class="msg-role">${role}</div>
        <div class="msg-pills"></div>
        <div class="msg-body">${content}</div>
    `;
    msgs.appendChild(div);
    return div;
}


const PILL_COLLAPSE_THRESHOLD = 3;

export function appendPill(msgDiv, text, state = 'active') {
    const pills = msgDiv.querySelector('.msg-pills');
    const pill = document.createElement('div');
    pill.className = `search-pill ${state}`;
    pill.innerHTML = state === 'active'
        ? `<div class="spinner"></div>${text}`
        : `✓ ${text}`;

    // Count existing visible pills (exclude the toggle and the collapsible wrapper)
    const visiblePills = pills.querySelectorAll(':scope > .search-pill');
    const count = visiblePills.length;

    if (count < PILL_COLLAPSE_THRESHOLD) {
        // Still under threshold — just append directly
        pills.appendChild(pill);
    } else {
        // We need the collapsible container
        let wrapper = pills.querySelector('.pills-collapsible');
        let toggle = pills.querySelector('.pills-toggle');

        if (!wrapper) {
            // Create toggle button
            toggle = document.createElement('button');
            toggle.className = 'pills-toggle';
            toggle.setAttribute('aria-expanded', 'false');
            toggle.addEventListener('click', () => {
                const expanded = wrapper.classList.toggle('open');
                toggle.setAttribute('aria-expanded', String(expanded));
                updateToggleLabel(toggle, wrapper);
            });
            pills.appendChild(toggle);

            // Create collapsible wrapper
            wrapper = document.createElement('div');
            wrapper.className = 'pills-collapsible';
            pills.appendChild(wrapper);
        }

        wrapper.appendChild(pill);
        updateToggleLabel(toggle, wrapper);
    }

    return pill;
}

function updateToggleLabel(toggle, wrapper) {
    const count = wrapper.children.length;
    const isOpen = wrapper.classList.contains('open');
    toggle.innerHTML = isOpen
        ? `▾ hide ${count} more`
        : `▸ ${count} more steps…`;
}

export function renderLinkCard(container, card) {
    const el = document.createElement('a');
    el.className = 'link-card';
    el.href = card.url;
    el.target = '_blank';
    el.rel = 'noopener noreferrer';

    const domain = new URL(card.url).hostname;
    el.innerHTML = `
        <div class="link-card-header">
            <img class="link-card-favicon" src="${card.favicon || `https://www.google.com/s2/favicons?domain=${domain}`}" 
                 onerror="this.style.display='none'" />
            <span class="link-card-domain">${domain}</span>
        </div>
        <div class="link-card-title">${escHtml(card.title || domain)}</div>
        ${card.description ? `<div class="link-card-desc">${escHtml(card.description)}</div>` : ''}
        ${card.image ? `<img class="link-card-img" src="${card.image}" onerror="this.style.display='none'" />` : ''}
    `;
    container.appendChild(el);
}

export function renderActionButton(container, action) {
    const btn = document.createElement('button');
    btn.className = 'action-button';
    
    if (action.type === 'mail') {
        const { to, subject, body, cc, bcc } = action.data;
        btn.innerHTML = `
            <svg class="action-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"></path>
                <polyline points="22,6 12,13 2,6"></polyline>
            </svg>
            <span>Draft Email</span>
        `;
        btn.addEventListener('click', () => {
            const params = new URLSearchParams();
            if (subject) params.append('subject', subject);
            if (body) params.append('body', body);
            if (cc && cc.length) params.append('cc', cc.join(','));
            if (bcc && bcc.length) params.append('bcc', bcc.join(','));
            
            const url = `mailto:${encodeURIComponent(to)}?${params.toString().replace(/\+/g, '%20')}`;
            window.open(url, '_blank');
        });
    } else {
        btn.textContent = action.label || 'Action';
    }

    container.appendChild(btn);
}

export function renderMarkdown(mdDiv, text) {
    mdDiv.innerHTML = marked.parse(text);

    mdDiv.querySelectorAll('a').forEach(a => {
        a.setAttribute('target', '_blank');
        a.setAttribute('rel', 'noopener noreferrer');
    });

    mdDiv.querySelectorAll('pre').forEach(pre => {
        if (pre.querySelector('.copy-btn')) return;
        const btn = document.createElement('button');
        btn.className = 'copy-btn';
        btn.textContent = 'copy';
        btn.addEventListener('click', () => {
            const code = pre.querySelector('code')?.innerText ?? '';
            navigator.clipboard.writeText(code).then(() => {
                btn.textContent = '✓';
                btn.classList.add('copied');
                setTimeout(() => {
                    btn.textContent = 'copy';
                    btn.classList.remove('copied');
                }, 1500);
            });
        });
        pre.style.position = 'relative';
        pre.appendChild(btn);
    });
}

/**
 * Create (or return existing) a collapsible thinking block inside msgBody.
 * Call this once when the first thinking_token arrives; subsequent tokens
 * are appended to the returned inner <pre> element via `thinkingBlock.contentEl`.
 */
export function ensureThinkingBlock(msgBody) {
    let block = msgBody.querySelector('.thinking-block');
    if (block) return block;

    const details = document.createElement('details');
    details.className = 'thinking-block';

    const summary = document.createElement('summary');
    summary.className = 'thinking-summary';
    summary.textContent = '💭 thinking…';
    details.appendChild(summary);

    const pre = document.createElement('pre');
    pre.className = 'thinking-content';
    details.appendChild(pre);

    // Insert before the md-content div so thinking appears above the answer
    const mdContent = msgBody.querySelector('.md-content');
    msgBody.insertBefore(details, mdContent);

    details.contentEl = pre;
    return details;
}

/**
 * Append a line of Ollama verbose stats below the message body.
 * @param {HTMLElement} msgBody
 * @param {object} stats  — { tokens_per_sec, eval_count, prompt_eval_count, load_duration_ms, total_duration_ms }
 */
export function appendOllamaStats(msgBody, stats) {
    const parts = [];
    if (stats.tokens_per_sec != null)     parts.push(`${stats.tokens_per_sec} tok/s`);
    if (stats.eval_count != null)         parts.push(`${stats.eval_count} tokens`);
    if (stats.prompt_eval_count != null)  parts.push(`${stats.prompt_eval_count} prompt tokens`);
    if (stats.total_duration_ms != null)  parts.push(`${(stats.total_duration_ms / 1000).toFixed(2)}s total`);
    if (stats.load_duration_ms != null && stats.load_duration_ms > 10)
        parts.push(`${stats.load_duration_ms}ms load`);

    if (parts.length === 0) return;

    const el = document.createElement('div');
    el.className = 'ollama-stats';
    el.textContent = parts.join(' · ');
    msgBody.appendChild(el);
}
