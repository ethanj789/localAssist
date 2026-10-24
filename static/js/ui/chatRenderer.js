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

/**
 * Render image thumbnails inside a message's body. Accepts a list of image
 * sources — either data URLs (freshly sent) or /chat-image/<path> URLs
 * (restored from history). Thumbnails are prepended above any text content.
 *
 * @param {HTMLElement} msgDiv - the .msg container returned by appendMsg
 * @param {string[]} sources - image URLs/data URLs to render
 */
export function appendMsgImages(msgDiv, sources) {
    if (!msgDiv || !sources || !sources.length) return;
    const body = msgDiv.querySelector('.msg-body');
    if (!body) return;

    const strip = document.createElement('div');
    strip.className = 'msg-images';
    sources.forEach(src => {
        // Anchor is kept (href = full image) so middle-click / ctrl-click and
        // "open in new tab" keep working natively. Left click is intercepted
        // to open the in-window lightbox instead of navigating.
        const a = document.createElement('a');
        a.href = src;
        a.target = '_blank';
        a.rel = 'noopener';
        a.className = 'msg-image';

        const img = document.createElement('img');
        img.src = src;
        img.alt = 'attached image';
        img.loading = 'lazy';
        a.appendChild(img);

        a.addEventListener('click', (e) => {
            // Let the browser handle modified clicks (ctrl/cmd/shift/middle) so
            // "open in new tab" still works. Plain left click → lightbox.
            if (e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
            e.preventDefault();
            openImageLightbox(src);
        });

        strip.appendChild(a);
    });
    body.insertBefore(strip, body.firstChild);
}

/**
 * Render a thumbnail strip of image sources into a message body (shared by the
 * image-thumbnail paths). `sources` is an array of image URLs/data URLs.
 * Returns the created strip element (or null) so callers can group content.
 */
function buildImageStrip(sources) {
    const strip = document.createElement('div');
    strip.className = 'msg-images';
    sources.forEach(src => {
        const a = document.createElement('a');
        a.href = src;
        a.target = '_blank';
        a.rel = 'noopener';
        a.className = 'msg-image';

        const img = document.createElement('img');
        img.src = src;
        img.alt = 'attached page';
        img.loading = 'lazy';
        a.appendChild(img);

        a.addEventListener('click', (e) => {
            if (e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
            e.preventDefault();
            openImageLightbox(src);
        });
        strip.appendChild(a);
    });
    return strip;
}

/**
 * Render chat attachments (images AND non-image files like PDFs) above a
 * message's text. Generalizes appendMsgImages. Each item describes one
 * attachment and comes in one of two shapes:
 *
 *   Optimistic send (from the composer):
 *     { name, dataUrl, isImage }
 *   Restored from history (a stored file object, enriched with URLs by main.js):
 *     { type, name, pages, imageUrls: [url,...], originalUrl }
 *
 * Images render as thumbnails; non-image files render as a named chip linking
 * to the original (download). For PDFs restored from history, the rendered page
 * images are shown as thumbnails and the chip links to the original PDF.
 *
 * @param {HTMLElement} msgDiv
 * @param {Array<Object|string>} items
 */
export function appendMsgAttachments(msgDiv, items) {
    if (!msgDiv || !items || !items.length) return;
    const body = msgDiv.querySelector('.msg-body');
    if (!body) return;

    const container = document.createElement('div');
    container.className = 'msg-attachments';

    const imageSources = [];   // thumbnails to show
    const fileChips = [];      // {name, href}

    items.forEach(item => {
        // Plain string = an image URL/data URL (back-compat).
        if (typeof item === 'string') {
            imageSources.push(item);
            return;
        }
        if (!item || typeof item !== 'object') return;

        const isImage = item.isImage === true || item.type === 'image'
            || (typeof item.dataUrl === 'string' && item.dataUrl.startsWith('data:image/'));

        // Thumbnails only for actual images: optimistic image dataUrl, or
        // restored page image URLs. Document page renders (PDF, etc.) are NOT
        // shown as thumbnails — those files get a named chip only.
        if (item.dataUrl && isImage) {
            imageSources.push(item.dataUrl);
        }
        if (isImage && Array.isArray(item.imageUrls)) {
            item.imageUrls.forEach(u => imageSources.push(u));
        }

        // Non-image files get a named chip. Link only to a real stored-file
        // URL (originalUrl) — never to a raw base64 data URL, which is huge and
        // not a meaningful link. On optimistic send the file isn't persisted
        // yet, so the chip is just a label until the conversation is reopened.
        if (!isImage) {
            fileChips.push({
                name: item.name || 'file',
                href: item.originalUrl || null,
                pages: item.pages,
            });
        }
    });

    if (imageSources.length) {
        container.appendChild(buildImageStrip(imageSources));
    }

    if (fileChips.length) {
        const row = document.createElement('div');
        row.className = 'msg-file-chips';
        fileChips.forEach(fc => {
            const el = fc.href ? document.createElement('a') : document.createElement('div');
            el.className = 'msg-file-chip';
            // Tag with the file name so an optimistic (link-less) chip can be
            // upgraded to a real link once the server reports the stored file.
            el.dataset.fileName = fc.name || 'file';
            if (fc.href) {
                el.href = fc.href;
                el.target = '_blank';
                el.rel = 'noopener';
            }
            const icon = document.createElement('span');
            icon.className = 'msg-file-chip-icon';
            icon.textContent = '📄';
            el.appendChild(icon);

            const label = document.createElement('span');
            label.className = 'msg-file-chip-name';
            label.textContent = fc.name;
            label.title = fc.name;
            el.appendChild(label);

            row.appendChild(el);
        });
        container.appendChild(row);
    }

    if (container.childNodes.length) {
        body.insertBefore(container, body.firstChild);
    }
}

/**
 * Upgrade a message's optimistic (link-less) file chips into real links once
 * the server reports the persisted file objects. Matches chips to file objects
 * by name and replaces the placeholder <div> with an <a> that opens the stored
 * original in a new tab — the same behavior a reloaded conversation gives.
 *
 * @param {HTMLElement} msgDiv  the user message div holding the chips
 * @param {string} conversationId
 * @param {Array<{name, path, ext}>} files  stored file objects from the server
 */
export function upgradeFileChipLinks(msgDiv, conversationId, files) {
    if (!msgDiv || !conversationId || !Array.isArray(files)) return;
    const chips = msgDiv.querySelectorAll('.msg-file-chip');
    files.forEach(f => {
        if (!f || !f.ext || !f.path) return;
        const url = `/chat-file/${conversationId}/raw/${f.path}.${f.ext}`;
        // Find a not-yet-linked chip with a matching name.
        for (const chip of chips) {
            if (chip.tagName === 'A') continue; // already linked
            if (chip.dataset.fileName !== (f.name || 'file')) continue;
            const a = document.createElement('a');
            a.className = chip.className;
            a.dataset.fileName = chip.dataset.fileName;
            a.href = url;
            a.target = '_blank';
            a.rel = 'noopener';
            while (chip.firstChild) a.appendChild(chip.firstChild);
            chip.replaceWith(a);
            break;
        }
    });
}

/**
 * Open a fullscreen, in-window lightbox showing the given image.
 * Click the backdrop or press Escape to close. A single reusable overlay
 * element is kept in the DOM.
 *
 * @param {string} src - image URL to display
 */
export function openImageLightbox(src) {
    let overlay = document.getElementById('image-lightbox');
    if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'image-lightbox';
        overlay.className = 'image-lightbox';
        overlay.innerHTML = `
            <button class="image-lightbox-close" aria-label="Close">×</button>
            <img class="image-lightbox-img" alt="attached image" />
        `;
        document.body.appendChild(overlay);

        const close = () => overlay.classList.remove('open');

        // Clicking the backdrop (but not the image) closes it.
        overlay.addEventListener('click', (e) => {
            if (e.target === overlay || e.target.classList.contains('image-lightbox-close')) {
                close();
            }
        });

        // Escape closes while the overlay is open.
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && overlay.classList.contains('open')) close();
        });

        overlay._close = close;
    }

    overlay.querySelector('.image-lightbox-img').src = src;
    overlay.classList.add('open');
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

/** Find or create the trailing .pill-group in a message's flow (shared by the
 *  edit pills so they land in the same group as tool pills). */
function getPillGroup(msgDiv) {
    const flow = msgDiv.querySelector('.msg-flow');
    let group = flow.querySelector('.pill-group:last-child');
    if (!group || group.nextElementSibling) {
        group = document.createElement('div');
        group.className = 'pill-group';
        flow.appendChild(group);
    }
    return group;
}

/**
 * Append an expandable pill for a proposed edit, showing the full args the model
 * tried (action, path, anchor, content). The summary line is always visible;
 * clicking expands the anchor/content detail. `info` is the `edit_proposed`
 * payload: { tool, path, action, anchor, content, summary }.
 */
export function appendEditPill(msgDiv, info) {
    const group = getPillGroup(msgDiv);

    const details = document.createElement('details');
    details.className = 'edit-pill';

    const action = info.action || 'edit';
    const path = info.path || '(path from last read)';
    const summary = document.createElement('summary');
    summary.className = 'edit-pill-summary';
    summary.innerHTML =
        `✎ <span class="edit-pill-action">${escHtml(action)}</span> ` +
        `<span class="edit-pill-path">${escHtml(path)}</span>` +
        (info.summary ? ` <span class="edit-pill-note">— ${escHtml(info.summary)}</span>` : '');
    details.appendChild(summary);

    const body = document.createElement('div');
    body.className = 'edit-pill-body';
    if (info.anchor) {
        body.appendChild(makeEditField('anchor', info.anchor));
    }
    if (action !== 'delete' && info.content) {
        body.appendChild(makeEditField(action === 'create' ? 'content (new file)' : 'content', info.content));
    }
    if (!body.children.length) {
        const empty = document.createElement('div');
        empty.className = 'edit-pill-field-empty';
        empty.textContent = 'no anchor/content';
        body.appendChild(empty);
    }
    details.appendChild(body);

    group.appendChild(details);
    return details;
}

function makeEditField(label, value) {
    const wrap = document.createElement('div');
    wrap.className = 'edit-pill-field';
    const lab = document.createElement('div');
    lab.className = 'edit-pill-field-label';
    lab.textContent = label;
    const pre = document.createElement('pre');
    pre.className = 'edit-pill-field-value';
    pre.textContent = value;
    wrap.appendChild(lab);
    wrap.appendChild(pre);
    return wrap;
}

/**
 * Append a distinct failed-state pill when an edit is auto-rejected at the gate
 * (bad/duplicate anchor). Shows the specific error and the retry attempt so the
 * user can see the model is trying and failing. `info` is the `edit_rejected`
 * payload: { path, action, error, attempt, max_attempts }.
 */
export function appendEditRejectPill(msgDiv, info) {
    const group = getPillGroup(msgDiv);
    const pill = document.createElement('div');
    pill.className = 'search-pill edit-reject-pill';
    const attempt = info.attempt && info.max_attempts
        ? ` (retry ${info.attempt}/${info.max_attempts})`
        : '';
    pill.innerHTML =
        `✗ edit auto-rejected${escHtml(attempt)}: ` +
        `<span class="edit-reject-error">${escHtml(info.error || 'could not resolve')}</span>`;
    group.appendChild(pill);
    return pill;
}

/**
 * Build the human label for a tool request pill from the tool name + parsed
 * args. Shared by the live SSE path (tool_requested) and the reload path so the
 * two never drift. `args` is the already-parsed arguments object.
 */
export function toolRequestLabel(tool, args = {}) {
    switch (tool) {
        case 'web_search':
            return `tool requested: web_search "${args?.query || ''}"`;
        case 'fetch_webpage':
            return `tool requested: fetch_webpage ${args?.url || ''}`;
        case 'list_files':
            return `tool requested: list_files ${args?.subdir || args?.topic || '/'}`;
        case 'read_file':
            return `tool requested: read_file ${args?.path || ''}`;
        case 'read_code_skeleton':
            return `tool requested: read_code_skeleton for file: ${args?.path || ''}`;
        case 'search_semantic':
            return `tool requested: conceptual search: "${args?.query || ''}"`;
        case 'calculate':
            return `tool requested: calculate ${args?.expression || ''}`;
        default:
            return `tool requested: ${tool}`;
    }
}

/**
 * Append a compact pill for a stored tool result (reload path). Keeps the
 * transcript readable: edit outcomes get a clear applied/rejected/invalid pill;
 * other tool results collapse to a short "got result" line with the full text
 * available on expand. `result` is the raw tool-result string from the DB.
 */
export function appendToolResultPill(msgDiv, name, result) {
    const group = getPillGroup(msgDiv);
    const text = (result || '').trim();

    // Legacy outcomes from the old decoupled edit model (pre break-and-resume).
    if (text === 'APPROVED' || text.startsWith('REJECTED')) {
        const pill = document.createElement('div');
        const approved = text === 'APPROVED';
        pill.className = approved ? 'search-pill done edit-result-applied' : 'search-pill edit-reject-pill';
        pill.innerHTML = `${approved ? '✓' : '✗'} edit ${escHtml(text)}`;
        group.appendChild(pill);
        return pill;
    }

    // Edit outcomes (propose_edit results) — surface the state distinctly.
    if (name === 'propose_edit' || /^(Applied:|Edit rejected|Edit NOT applied)/.test(text)) {
        const pill = document.createElement('div');
        if (text.startsWith('Applied:')) {
            pill.className = 'search-pill done edit-result-applied';
            pill.innerHTML = `✓ ${escHtml(text)}`;
        } else {
            // "Edit rejected..." or "Edit NOT applied — ..." (rejected/stale/invalid)
            pill.className = 'search-pill edit-reject-pill';
            pill.innerHTML = `✗ ${escHtml(text)}`;
        }
        group.appendChild(pill);
        return pill;
    }

    if (!text) return null;

    // Generic tool result — short, expandable so it doesn't flood the reload.
    const details = document.createElement('details');
    details.className = 'tool-result-pill';
    const summary = document.createElement('summary');
    summary.className = 'tool-result-summary';
    summary.textContent = `✓ ${name} result`;
    details.appendChild(summary);
    const pre = document.createElement('pre');
    pre.className = 'tool-result-body';
    pre.textContent = text;
    details.appendChild(pre);
    group.appendChild(details);
    return details;
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
