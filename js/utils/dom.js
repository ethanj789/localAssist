export function escHtml(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

export function setInputDisplay(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
}

export function setStatus(ok, text) {
    const dot = document.getElementById('status-dot');
    if (dot) {
        dot.className = 'dot' + (ok ? ' green' : '');
    }
    const statusText = document.getElementById('status-text');
    if (statusText) {
        statusText.textContent = text;
    }
}

export function showToast(msg) {
    const t = document.getElementById('toast');
    if (t) {
        t.textContent = msg;
        t.classList.add('show');
        setTimeout(() => t.classList.remove('show'), 2000);
    }
}
