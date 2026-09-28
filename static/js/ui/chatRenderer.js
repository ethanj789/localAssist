import { escHtml } from '../utils/dom.js';

export function appendMsg(role, content, id) {
    const msgs = document.getElementById('messages');
    const div = document.createElement('div');
    div.className = `msg ${role}`;
    if (id) div.id = id;
    div.innerHTML = `
        <div class="msg-role">${role}</div>
        <div class="msg-flow"></div>
        <div class="msg-body">${content}</div>
    `;
    msgs.appendChild(div);
    return div;
}


const PILL_COLLAPSE_THRESHOLD = 3;

/**
 * Append a pill into the .msg-flow container (interleaved with thinking blocks).
 * Pills are grouped into a .pill-group — a new group is created after each thinking block.
 *
 * @param {HTMLElement} msgDiv - The .msg container
 * @param {string} text - Pill label
 * @param {string} state - 'active' or 'done'
 * @param {object} [opts] - Options
 * @param {boolean} [opts.collapsed] - If true, pill goes directly into collapsed section
 */
export function appendPill(msgDiv, text, state = 'active', opts = {}) {
    const flow = msgDiv.querySelector('.msg-flow');
    const pill = document.createElement('div');
    pill.className = `search-pill ${state}`;
    pill.innerHTML = state === 'active'
        ? `<div class="spinner"></div>${text}`
        : `✓ ${text}`;

    // Find or create the current pill-group (always the last .pill-group in flow)
    let group = flow.querySelector('.pill-group:last-child');
    if (!group || group.nextElementSibling) {
        // No group yet, or the last child in flow isn't a pill-group (it's a thinking block)
        group = document.createElement('div');
        group.className = 'pill-group';
        flow.appendChild(group);
    }

    // If this pill should be collapsed by default (status/plumbing pills),
    // always put it in the collapsible wrapper
    if (opts.collapsed) {
        let wrapper = group.querySelector('.pills-collapsible');
        let toggle = group.querySelector('.pills-toggle');

        if (!toggle) {
            toggle = document.createElement('button');
            toggle.className = 'pills-toggle';
            toggle.setAttribute('aria-expanded', 'false');
            toggle.addEventListener('click', () => {
                const expanded = wrapper.classList.toggle('open');
                toggle.setAttribute('aria-expanded', String(expanded));
                updateToggleLabel(toggle, wrapper);
            });
            // Insert toggle before any visible pills if they exist, or just append
            const firstVisible = group.querySelector(':scope > .search-pill');
            if (firstVisible) {
                group.insertBefore(toggle, firstVisible);
            } else {
                group.appendChild(toggle);
            }

            wrapper = document.createElement('div');
            wrapper.className = 'pills-collapsible';
            toggle.after(wrapper);
        }

        wrapper.appendChild(pill);
        updateToggleLabel(toggle, wrapper);
        return pill;
    }

    // Visible pills — count existing and collapse if over threshold
    const visiblePills = group.querySelectorAll(':scope > .search-pill');
    const count = visiblePills.length;

    if (count < PILL_COLLAPSE_THRESHOLD) {
        group.appendChild(pill);
    } else {
        let wrapper = group.querySelector('.pills-collapsible');
        let toggle = group.querySelector('.pills-toggle');

        if (!wrapper) {
            toggle = document.createElement('button');
            toggle.className = 'pills-toggle';
            toggle.setAttribute('aria-expanded', 'false');
            toggle.addEventListener('click', () => {
                const expanded = wrapper.classList.toggle('open');
                toggle.setAttribute('aria-expanded', String(expanded));
                updateToggleLabel(toggle, wrapper);
            });
            group.appendChild(toggle);

            wrapper = document.createElement('div');
            wrapper.className = 'pills-collapsible';
            group.appendChild(wrapper);
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

/**
 * Run KaTeX auto-render over an already-rendered markdown element.
 * Only $$...$$ (display) and \[...\] are treated as math — single $ is
 * intentionally left alone to avoid mangling prose like "$5 to $10".
 * Safe to call repeatedly; call it once rendering is final (not per-token)
 * since typesetting the whole subtree on every streamed token is expensive.
 */
export function renderMath(el) {
    if (typeof renderMathInElement !== 'function') return; // KaTeX not loaded
    try {
        renderMathInElement(el, {
            delimiters: [
                // Display math first so $$...$$ wins over $...$.
                { left: '$$', right: '$$', display: true },
                { left: '\\[', right: '\\]', display: true },
                // Inline math.
                { left: '$', right: '$', display: false },
                { left: '\\(', right: '\\)', display: false },
            ],
            throwOnError: false,
            ignoredTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code'],
        });
    } catch (e) {
        // Never let a math-parse failure break the whole message render.
        console.warn('KaTeX render failed', e);
    }
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
 * Create a NEW thinking block appended to the .msg-flow container.
 * Each time thinking resumes after a tool call, a new block is created
 * so that thinking and tool pills interleave chronologically.
 *
 * @param {HTMLElement} flowContainer  The .msg-flow element
 * @returns {HTMLElement} The <details> element with a .contentEl property
 */
export function createThinkingBlock(flowContainer) {
    const details = document.createElement('details');
    details.className = 'thinking-block';

    const summary = document.createElement('summary');
    summary.className = 'thinking-summary';
    summary.textContent = '💭 thinking…';
    details.appendChild(summary);

    const content = document.createElement('div');
    content.className = 'thinking-content md-content';
    // Raw markdown source accumulates here as tokens stream in.
    content._raw = '';
    details.appendChild(content);

    flowContainer.appendChild(details);

    details.contentEl = content;
    return details;
}

/**
 * Legacy compat: ensure at least one thinking block exists in a msg-body.
 * Delegates to createThinkingBlock using the .msg-flow inside the parent msg.
 */
export function ensureThinkingBlock(msgBody) {
    const msgDiv = msgBody.closest('.msg');
    const flow = msgDiv.querySelector('.msg-flow');
    let block = flow.querySelector('.thinking-block:last-child');
    if (block) return block;
    return createThinkingBlock(flow);
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
