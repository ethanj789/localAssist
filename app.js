const API = 'http://localhost:8000'
let isStreaming = false
let currentUseGroq = false
const THEMES = {
    default: { '--bg': '#0b0b0b', '--surface': '#181818', '--border': '#2d2d2d', '--text': '#f8f8f8', '--muted': '#d0d0d0', '--accent': '#7cffb6', '--accent2': '#7fb7ff', '--warn': '#ff8c56', '--sans': "'DM Sans', sans-serif" },
    light: { '--bg': '#f9f7f4', '--surface': '#ffffff', '--border': '#e0dbd4', '--text': '#1a1816', '--muted': '#6b6560', '--accent': '#0d6efd', '--accent2': '#6f42c1', '--warn': '#e86c00', '--sans': "'Plus Jakarta Sans', sans-serif" },
    solarized: { '--bg': '#002b36', '--surface': '#073642', '--border': '#094652', '--text': '#fdf6e3', '--muted': '#93a1a1', '--accent': '#2aa198', '--accent2': '#268bd2', '--warn': '#cb4b16', '--sans': "'Plus Jakarta Sans', sans-serif" },
    rose: { '--bg': '#191724', '--surface': '#1f1d2e', '--border': '#403d52', '--text': '#e0def4', '--muted': '#908caa', '--accent': '#ebbcba', '--accent2': '#9ccfd8', '--warn': '#f6c177', '--sans': "'Plus Jakarta Sans', sans-serif" },
    nord: { '--bg': '#2e3440', '--surface': '#3b4252', '--border': '#434c5e', '--text': '#eceff4', '--muted': '#d8dee9', '--accent': '#88c0d0', '--accent2': '#81a1c1', '--warn': '#ebcb8b', '--sans': "'Plus Jakarta Sans', sans-serif" },
    gruvbox: { '--bg': '#282828', '--surface': '#3c3836', '--border': '#504945', '--text': '#ebdbb2', '--muted': '#bdae93', '--accent': '#b8bb26', '--accent2': '#83a598', '--warn': '#fe8019', '--sans': "'Plus Jakarta Sans', sans-serif" },
    dracula: { '--bg': '#282a36', '--surface': '#3c3f4f', '--border': '#5c5c5c', '--text': '#f8f8f8', '--muted': '#8c8c8c', '--accent': '#8be9fd', '--accent2': '#50fa7b', '--warn': '#ff5555', '--sans': "'Plus Jakarta Sans', sans-serif" },
    nebula: { '--bg': '#1a1d23', '--surface': '#2c2f3f', '--border': '#4c4c4c', '--text': '#f8f8f8', '--muted': '#8c8c8c', '--accent': '#03a9f4', '--accent2': '#4caf50', '--warn': '#ff9800', '--sans': "'Plus Jakarta Sans', sans-serif" },
    lumina: { '--bg': '#f7f7f7', '--surface': '#ffffff', '--border': '#e0e0e0', '--text': '#1a1a1a', '--muted': '#666666', '--accent': '#2196f3', '--accent2': '#8bc34a', '--warn': '#ff5722', '--sans': "'Plus Jakarta Sans', sans-serif" },
    aurora: { '--bg': '#2f343a', '--surface': '#3c414f', '--border': '#5c5c5c', '--text': '#f8f8f8', '--muted': '#8c8c8c', '--accent': '#4caf50', '--accent2': '#03a9f4', '--warn': '#ff9800', '--sans': "'Plus Jakarta Sans', sans-serif" }
};

const FONTS = {
    dm: "'DM Sans', sans-serif",
    inter: "'Inter', sans-serif",
    nunito: "'Nunito', sans-serif",
    outfit: "'Outfit', sans-serif",
    jakarta: "'Plus Jakarta Sans', sans-serif",
};

const WEEK_MS = 7 * 24 * 60 * 60 * 1000;

function lsSet(key, value) {
    localStorage.setItem(key, JSON.stringify({ value, ts: Date.now() }));
}

function lsGet(key, fallback = null) {
    try {
        const raw = localStorage.getItem(key);
        if (!raw) return fallback;
        const { value, ts } = JSON.parse(raw);
        if (Date.now() - ts > WEEK_MS) {
            localStorage.removeItem(key);
            return fallback;
        }
        return value;
    } catch {
        return fallback;
    }
}

function setInputDisplay(id, value) {
    const el = document.getElementById(id)
    if (el) el.textContent = value
}

async function init() {
    await checkServerRestart()
    bindInputEvents()
    bindButtons()
    await loadConfig()
}

async function checkServerRestart() {
    try {
        const r = await fetch(`${API}/startup-token`)
        const { token } = await r.json()
        const stored = sessionStorage.getItem('startup-token')
        if (stored && stored !== token) {
            // server restarted, clear UI
            document.getElementById('messages').innerHTML = ''
            showToast('server restarted — history cleared')
        }
        sessionStorage.setItem('startup-token', token)
    } catch { }
}
function bindInputEvents() {
    document.getElementById('cfg-max-searches').addEventListener('input', e => {
        setInputDisplay('max-searches-val', e.target.value)
    })

    document.getElementById('send-btn').addEventListener('click', sendMessage)
    document.getElementById('cfg-model').addEventListener('change', () => { })
}

function bindButtons() {
    document.querySelector('.btn-apply').addEventListener('click', applyConfig)
    document.querySelector('.btn-clear').addEventListener('click', clearHistory)
    document.getElementById('user-input').addEventListener('input', function () {
        this.style.height = 'auto'
        this.style.height = Math.min(this.scrollHeight, 140) + 'px'
    })
    document.getElementById('user-input').addEventListener('keydown', e => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault()
            sendMessage()
        }
    })
}
async function applyConfig() {
    const updates = {
        model: document.getElementById('cfg-model').value.trim(),
        max_searches: parseInt(document.getElementById('cfg-max-searches').value, 10),
        system_prompt: document.getElementById('cfg-system-prompt').value.trim(),
        use_groq: document.getElementById('cfg-use-groq').checked,
        groq_model: document.getElementById('cfg-groq-model').value.trim(),
    }

    const groqChanged = updates.use_groq !== currentUseGroq

    try {
        const r = await fetch(`${API}/config`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(updates),
        })
        if (!r.ok) throw new Error('failed to apply config')

        if (groqChanged) {
            await fetch(`${API}/history`, { method: 'DELETE' })
            document.getElementById('messages').innerHTML = ''
            showToast('config applied — history cleared (backend switched)')
        } else {
            showToast('config applied')
        }
        currentUseGroq = updates.use_groq
    } catch (err) {
        showToast('failed to apply config')
    }
}
async function loadConfig() {
    try {
        const r = await fetch(`${API}/config`)
        if (!r.ok) throw new Error('failed to load config')
        const cfg = await r.json()
        document.getElementById('cfg-model').value = cfg.model || 'gemma4:2b'
        document.getElementById('cfg-max-searches').value = cfg.max_searches || 5
        setInputDisplay('max-searches-val', cfg.max_searches || 5)
        if (cfg.system_prompt) document.getElementById('cfg-system-prompt').value = cfg.system_prompt
        setStatus(true, 'connected')
        if (cfg.groq_model) document.getElementById('cfg-groq-model').value = cfg.groq_model
        document.getElementById('cfg-use-groq').checked = cfg.use_groq || false
        currentUseGroq = cfg.use_groq || false
    } catch (err) {
        setStatus(false, 'server offline')
    }
}

function setStatus(ok, text) {
    const dot = document.getElementById('status-dot')
    dot.className = 'dot' + (ok ? ' green' : '')
    document.getElementById('status-text').textContent = text
}

// async function applyConfig() {
//     const updates = {
//         model: document.getElementById('cfg-model').value.trim(),
//         max_searches: parseInt(document.getElementById('cfg-max-searches').value, 10),
//         system_prompt: document.getElementById('cfg-system-prompt').value.trim(),
//         use_groq: document.getElementById('cfg-use-groq').checked,
//         groq_model: document.getElementById('cfg-groq-model').value.trim(),
//     }

//     try {
//         const r = await fetch(`${API}/config`, {
//             method: 'POST',
//             headers: { 'Content-Type': 'application/json' },
//             body: JSON.stringify(updates),
//         })
//         if (!r.ok) throw new Error('failed to apply config')
//         showToast('config applied')
//     } catch (err) {
//         showToast('failed to apply config')
//     }
// }

async function clearHistory() {
    await fetch(`${API}/history`, { method: 'DELETE' })
    document.getElementById('messages').innerHTML = ''
    showToast('history cleared')
}

function appendMsg(role, content, id) {
    const msgs = document.getElementById('messages')
    const div = document.createElement('div')
    div.className = `msg ${role}`
    if (id) div.id = id
    div.innerHTML = `
        <div class="msg-role">${role}</div>
        <div class="msg-pills"></div>
        <div class="msg-body">${content}</div>
    `
    msgs.appendChild(div)
    msgs.scrollTop = msgs.scrollHeight
    return div
}

function appendPill(msgDiv, text, state = 'active') {
    const pills = msgDiv.querySelector('.msg-pills')  // ← goes into pills div, above body
    const pill = document.createElement('div')
    pill.className = `search-pill ${state}`
    pill.innerHTML = state === 'active'
        ? `<div class="spinner"></div>${text}`
        : `✓ ${text}`
    pills.appendChild(pill)
    return pill
}

async function sendMessage() {
    let linkCards = []

    if (isStreaming) return
    const input = document.getElementById('user-input')
    const text = input.value.trim()
    if (!text) return

    input.value = ''
    input.style.height = 'auto'
    isStreaming = true
    document.getElementById('send-btn').disabled = true
    setStatus(true, 'thinking...')

    appendMsg('user', escHtml(text))

    const aId = 'msg-' + Date.now()
    const aDiv = appendMsg('assistant', '', aId)
    const aBody = aDiv.querySelector('.msg-body')
    const mdDiv = document.createElement('div')
    mdDiv.className = 'md-content'
    let cursor = document.createElement('span')
    cursor.className = 'cursor'
    aBody.appendChild(mdDiv)
    aBody.appendChild(cursor)

    let currentPill = null
    let fullText = ''

    try {
        const resp = await fetch(`${API}/chat`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ message: text }),
        })

        const reader = resp.body.getReader()
        const decoder = new TextDecoder()
        let buf = ''

        while (true) {
            const { done, value } = await reader.read()
            if (done) break
            buf += decoder.decode(value, { stream: true })

            const lines = buf.split('\n\n')
            buf = lines.pop()

            for (const line of lines) {
                if (!line.startsWith('data: ')) continue
                let parsed
                try { parsed = JSON.parse(line.slice(6)) } catch { continue }

                const { event, data } = parsed

                if (event === 'token') {
                    fullText += data
                    renderMarkdown(mdDiv, fullText)
                    document.getElementById('messages').scrollTop = 9999
                }

                if (event === 'tool_requested') {
                    const info = JSON.parse(data)
                    let label
                    if (info.tool === 'web_search') {
                        label = `tool requested: web_search "${info.args?.query || ''}"`
                    } else if (info.tool === 'fetch_webpage') {
                        label = `tool requested: fetch_webpage ${info.args?.url || ''}`
                    } else if (info.tool === 'list_files') {
                        label = `tool requested: list_files ${info.args?.subdir || '/'}`
                    } else if (info.tool === 'read_file') {
                        label = `tool requested: read_file ${info.args?.path || ''}`
                    } else {
                        label = `tool requested: ${info.tool}`
                    }
                    appendPill(aDiv, label, 'done')
                }
                if (event === 'searching') {
                    const info = JSON.parse(data)
                    const label = info.label || (info.tool === 'web_search'
                        ? `searching: "${info.args?.query}" (${info.count}/${info.max})`
                        : `fetching: ${info.args?.url?.slice(0, 40)}...`)
                    currentPill = appendPill(aDiv, label, 'active')
                }

                if (event === 'search_result') {
                    if (currentPill) {
                        currentPill.className = 'search-pill done'
                        currentPill.innerHTML = `✓ ${currentPill.textContent.trim()}`
                    }
                }

                if (event === 'status') {
                    appendPill(aDiv, data, 'done')
                }

                if (event === 'model') {
                    const info = JSON.parse(data)
                    appendPill(
                        aDiv,
                        `model: ${info.provider}/${info.model}`,
                        'done'
                    )
                }
                if (event === 'link_card') {
                    linkCards.push(JSON.parse(data))
                }
                if (event === 'done') {
                    const info = JSON.parse(data)
                    cursor.remove()
                    if (info.searches_used > 0) {
                        const meta = document.createElement('div')
                        meta.className = 'meta'
                        meta.textContent = `${info.searches_used} search${info.searches_used !== 1 ? 'es' : ''} used`
                        aBody.appendChild(meta)
                    }
                    if (linkCards.length > 0) {
                        const tray = document.createElement('div')
                        tray.className = 'link-card-tray'
                        linkCards.forEach(card => renderLinkCard(tray, card))
                        aBody.appendChild(tray)
                        linkCards = []
                    }
                }
            }
        }
    } catch (e) {
        aBody.innerHTML = `<span style="color:var(--warn)">error: ${e.message}</span>`
        setStatus(false, 'error')
    } finally {
        cursor.remove()
        isStreaming = false
        document.getElementById('send-btn').disabled = false
        setStatus(true, 'ready')
    }
}

function escHtml(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

function showToast(msg) {
    const t = document.getElementById('toast')
    t.textContent = msg
    t.classList.add('show')
    setTimeout(() => t.classList.remove('show'), 2000)
}

function renderLinkCard(container, card) {
    const el = document.createElement('a')
    el.className = 'link-card'
    el.href = card.url
    el.target = '_blank'
    el.rel = 'noopener noreferrer'

    const domain = new URL(card.url).hostname
    el.innerHTML = `
        <div class="link-card-header">
            <img class="link-card-favicon" src="${card.favicon || `https://www.google.com/s2/favicons?domain=${domain}`}" 
                 onerror="this.style.display='none'" />
            <span class="link-card-domain">${domain}</span>
        </div>
        <div class="link-card-title">${escHtml(card.title || domain)}</div>
        ${card.description ? `<div class="link-card-desc">${escHtml(card.description)}</div>` : ''}
        ${card.image ? `<img class="link-card-img" src="${card.image}" onerror="this.style.display='none'" />` : ''}
    `
    container.appendChild(el)
}

function renderMarkdown(mdDiv, text) {
    mdDiv.innerHTML = marked.parse(text)
    mdDiv.querySelectorAll('pre').forEach(pre => {
        if (pre.querySelector('.copy-btn')) return
        const btn = document.createElement('button')
        btn.className = 'copy-btn'
        btn.textContent = 'copy'
        btn.addEventListener('click', () => {
            const code = pre.querySelector('code')?.innerText ?? ''
            navigator.clipboard.writeText(code).then(() => {
                btn.textContent = '✓'
                btn.classList.add('copied')
                setTimeout(() => {
                    btn.textContent = 'copy'
                    btn.classList.remove('copied')
                }, 1500)
            })
        })
        pre.style.position = 'relative'
        pre.appendChild(btn)
    })
}

function applyTheme(id) {
    const vars = THEMES[id] ?? THEMES.default;
    const root = document.documentElement;
    Object.entries(vars).forEach(([k, v]) => {
        if (k !== '--sans') root.style.setProperty(k, v); // ← skip --sans
    });
    lsSet('theme', id);
    // Re-apply the current font so it wins over anything the theme might've set
    applyFont(lsGet('font') ?? 'dm');
}

// Wire to your select
document.getElementById('theme-select').addEventListener('change', e => applyTheme(e.target.value));

// Restore saved theme on load
applyTheme(lsGet('theme') ?? 'default');

function applyFont(id) {
    document.documentElement.style.setProperty('--sans', FONTS[id] ?? FONTS.dm);
    lsSet('font', id);
}

document.getElementById('font-select').addEventListener('change', e => applyFont(e.target.value));

// restore on load
applyFont(lsGet('font') ?? 'dm');

init()
