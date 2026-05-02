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
    msgs.scrollTop = msgs.scrollHeight;
    return div;
}

export function appendPill(msgDiv, text, state = 'active') {
    const pills = msgDiv.querySelector('.msg-pills');
    const pill = document.createElement('div');
    pill.className = `search-pill ${state}`;
    pill.innerHTML = state === 'active'
        ? `<div class="spinner"></div>${text}`
        : `✓ ${text}`;
    pills.appendChild(pill);
    return pill;
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
