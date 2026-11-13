// Manages a single "armed" quote (highlighted page text from the side panel's
// active tab) staged for the next message. One live pill in the composer, × to
// drop, replaced on new selection, cleared on empty. Sent as a `type:"quote"`
// entry on the attachments channel (not a file — no bytes, no prep_file).

let staged = null; // { text, url, title, source } or null
const PREVIEW_CHARS = 140;

function previewEl() {
    return document.getElementById('image-attachment-preview');
}

export function getPendingQuote() {
    if (!staged || !staged.text) return null;
    return {
        type: 'quote',
        text: staged.text,
        source: staged.source || 'tab-highlight',
        url: staged.url || '',
        title: staged.title || '',
    };
}

export function hasPendingQuote() {
    return !!(staged && staged.text);
}

export function clearPendingQuote() {
    staged = null;
    renderQuotePill();
}

// Arm or replace the quote; empty text clears the pill (collapsed selection).
export function setQuote({ text, url, title, source } = {}) {
    const clean = (text || '').trim();
    if (!clean) {
        if (staged) { staged = null; renderQuotePill(); }
        return;
    }
    staged = {
        text: clean,
        url: url || '',
        title: title || '',
        source: source || 'tab-highlight',
    };
    renderQuotePill();
}

function truncate(s, n) {
    return s.length > n ? s.slice(0, n).trimEnd() + '…' : s;
}

// Render the single quote pill into the shared composer preview strip (which
// also holds attachment chips). Keeps `has-items` in sync with both.
function renderQuotePill() {
    const el = previewEl();
    if (!el) return;

    const existing = el.querySelector('.quote-chip');
    if (existing) existing.remove();

    if (!staged || !staged.text) {
        if (!el.children.length) el.classList.remove('has-items');
        return;
    }

    el.classList.add('has-items');

    const chip = document.createElement('div');
    chip.className = 'quote-chip';

    const icon = document.createElement('span');
    icon.className = 'quote-chip-icon';
    icon.textContent = '“';
    chip.appendChild(icon);

    const body = document.createElement('div');
    body.className = 'quote-chip-body';

    const textEl = document.createElement('div');
    textEl.className = 'quote-chip-text';
    textEl.textContent = truncate(staged.text, PREVIEW_CHARS);
    textEl.title = staged.text;
    body.appendChild(textEl);

    if (staged.title || staged.url) {
        const src = document.createElement('div');
        src.className = 'quote-chip-source';
        src.textContent = staged.title || staged.url;
        src.title = staged.url || staged.title;
        body.appendChild(src);
    }
    chip.appendChild(body);

    const rm = document.createElement('button');
    rm.className = 'image-chip-remove quote-chip-remove';
    rm.type = 'button';
    rm.title = 'Remove quote';
    rm.textContent = '×';
    rm.addEventListener('click', () => clearPendingQuote());
    chip.appendChild(rm);

    el.insertBefore(chip, el.firstChild);
}

// Receive selection relays from the side panel bridge (sidepanel.js).
export function initQuotes() {
    window.addEventListener('message', (e) => {
        const data = e.data;
        if (!data || typeof data.type !== 'string') return;
        if (data.type === 'localassist:selection') {
            if (typeof data.text !== 'string') return;
            setQuote({
                text: data.text,
                url: typeof data.url === 'string' ? data.url : '',
                title: typeof data.title === 'string' ? data.title : '',
                source: typeof data.source === 'string' ? data.source : 'tab-highlight',
            });
        } else if (data.type === 'localassist:selection-clear') {
            // Don't let a tab-switch clear wipe a non-tab quote (future replies).
            if (staged && staged.source && staged.source !== 'tab-highlight') return;
            setQuote({ text: '' });
        }
    });
    renderQuotePill();
}
