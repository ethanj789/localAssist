// MCP server panel: read + reload UI.
//
// Backend contract (already live in server.py):
//   GET  /mcp/servers  -> { servers: [ sanitized ] }
//   POST /mcp/reload   -> { status: "ok", servers: [ sanitized ] }
//
// A sanitized server is: { name, transport, enabled, order, command, args,
// url, last_status, last_error, tool_count }. Secrets (headers/env) are never
// sent to the client, so this module never sees or stores them.
//
// Config writes (add / delete / enable-disable) go through dedicated endpoints
// that persist to mcp_servers.json / mcp_meta.json and reload automatically:
//   POST   /mcp/servers         add a server, then reload
//   DELETE /mcp/servers/{name}  remove a server, then reload
//   POST   /mcp/servers/toggle  flip enabled (sidecar only), then reload
// Each returns the fresh sanitized { servers } list so the UI re-renders in one
// round-trip. Secrets submitted on add are never echoed back.

import { API } from '../constants.js';
import { showToast } from '../utils/dom.js';

const listEl = () => document.getElementById('mcp-server-list');

// ── rendering the server list ────────────────────────────────────────────────

const STATUS_META = {
    ok: { cls: 'ok', dot: '●', label: 'connected' },
    error: { cls: 'error', dot: '●', label: 'error' },
};

function statusMeta(server) {
    if (!server.enabled) return { cls: 'disabled', dot: '○', label: 'disabled' };
    const s = STATUS_META[server.last_status];
    if (s) return s;
    return { cls: 'unknown', dot: '○', label: 'not loaded' };
}

function escapeHtml(s) {
    return String(s ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

function renderServers(servers) {
    const el = listEl();
    if (!el) return;

    if (!servers || servers.length === 0) {
        el.innerHTML = `<div class="mcp-empty">no servers configured — add one below</div>`;
        return;
    }

    el.innerHTML = servers.map((srv) => {
        const st = statusMeta(srv);
        const target = srv.transport === 'stdio'
            ? escapeHtml([srv.command, ...(srv.args || [])].filter(Boolean).join(' '))
            : escapeHtml(srv.url || '');
        const toolCount = (srv.tool_count ?? null) !== null
            ? `${srv.tool_count} tool${srv.tool_count === 1 ? '' : 's'}`
            : '';
        const err = srv.last_status === 'error' && srv.last_error
            ? `<div class="mcp-server-error" title="${escapeHtml(srv.last_error)}">${escapeHtml(srv.last_error)}</div>`
            : '';
        const name = escapeHtml(srv.name);
        return `
            <div class="mcp-server" data-name="${name}" data-enabled="${srv.enabled ? '1' : '0'}">
                <div class="mcp-server-head">
                    <span class="mcp-status-dot ${st.cls}" title="${st.label}">${st.dot}</span>
                    <span class="mcp-server-name">${name}</span>
                    <span class="mcp-badge transport">${escapeHtml(srv.transport)}</span>
                    ${toolCount ? `<span class="mcp-badge tools">${toolCount}</span>` : ''}
                    <span class="mcp-server-actions">
                        <button class="mcp-icon-btn mcp-toggle" title="${srv.enabled ? 'disable' : 'enable'}">
                            ${srv.enabled ? 'on' : 'off'}
                        </button>
                        <button class="mcp-icon-btn mcp-delete" title="delete server">✕</button>
                    </span>
                </div>
                ${target ? `<div class="mcp-server-target" title="${target}">${target}</div>` : ''}
                ${err}
            </div>`;
    }).join('');

    // Wire per-row controls (delegating would also work; this keeps it local).
    el.querySelectorAll('.mcp-server').forEach((row) => {
        const name = row.getAttribute('data-name');
        const enabled = row.getAttribute('data-enabled') === '1';
        const toggleBtn = row.querySelector('.mcp-toggle');
        const deleteBtn = row.querySelector('.mcp-delete');
        if (toggleBtn) toggleBtn.addEventListener('click', () => toggleServer(name, !enabled));
        if (deleteBtn) deleteBtn.addEventListener('click', () => deleteServer(name));
    });
}

// ── write actions (each reloads server-side and returns the fresh list) ───────

function setListBusy(busy) {
    const el = listEl();
    if (el) el.classList.toggle('mcp-busy', busy);
}

async function toggleServer(name, enabled) {
    setListBusy(true);
    try {
        const r = await fetch(`${API}/mcp/servers/toggle`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, enabled }),
        });
        if (!r.ok) throw new Error();
        const data = await r.json();
        renderServers(data.servers || []);
        showToast(`${name} ${enabled ? 'enabled' : 'disabled'} — applies next turn`);
    } catch {
        showToast(`failed to ${enabled ? 'enable' : 'disable'} ${name}`);
    } finally {
        // renderServers() rewrites the list's innerHTML but not the container's
        // class, so the busy/dim flag must be cleared here regardless of outcome
        // — otherwise the list stays grayed out and pointer-events:none sticks.
        setListBusy(false);
    }
}

async function deleteServer(name) {
    if (!confirm(`Delete MCP server "${name}"? This removes it from mcp_servers.json.`)) return;
    setListBusy(true);
    try {
        const r = await fetch(`${API}/mcp/servers/${encodeURIComponent(name)}`, { method: 'DELETE' });
        if (!r.ok) throw new Error();
        const data = await r.json();
        renderServers(data.servers || []);
        showToast(`${name} removed`);
    } catch {
        showToast(`failed to delete ${name}`);
    } finally {
        setListBusy(false);
    }
}

async function loadServers() {
    const el = listEl();
    if (el) el.innerHTML = `<div class="mcp-empty">loading…</div>`;
    try {
        const r = await fetch(`${API}/mcp/servers`);
        if (!r.ok) throw new Error();
        const data = await r.json();
        renderServers(data.servers || []);
    } catch {
        if (el) el.innerHTML = `<div class="mcp-empty mcp-empty-err">failed to load servers</div>`;
    }
}

async function reloadServers() {
    const btn = document.getElementById('btn-mcp-reload');
    if (btn) btn.classList.add('spinning');
    try {
        const r = await fetch(`${API}/mcp/reload`, { method: 'POST' });
        if (!r.ok) throw new Error();
        const data = await r.json();
        renderServers(data.servers || []);
        showToast('MCP reloaded — new tools apply next turn');
    } catch {
        showToast('MCP reload failed');
    } finally {
        if (btn) btn.classList.remove('spinning');
    }
}

// ── add-server modal: build a config block (no write endpoint yet) ────────────

let currentTransport = 'stdio';

function parseLines(text) {
    return (text || '')
        .split('\n')
        .map((l) => l.trim())
        .filter(Boolean);
}

function parseKeyVals(text, sep) {
    const out = {};
    for (const line of parseLines(text)) {
        const i = line.indexOf(sep);
        if (i === -1) continue;
        const k = line.slice(0, i).trim();
        const v = line.slice(i + sep.length).trim();
        if (k) out[k] = v;
    }
    return out;
}

// Build { name, block } matching the backend's mcpServers shape and transport
// inference: `command` -> stdio; `url` (+ optional "type":"sse") -> remote.
function buildBlock() {
    const name = (document.getElementById('mcp-f-name').value || '').trim();
    const block = {};

    if (currentTransport === 'stdio') {
        const command = (document.getElementById('mcp-f-command').value || '').trim();
        const args = parseLines(document.getElementById('mcp-f-args').value);
        const env = parseKeyVals(document.getElementById('mcp-f-env').value, '=');
        if (command) block.command = command;
        if (args.length) block.args = args;
        if (Object.keys(env).length) block.env = env;
    } else {
        const url = (document.getElementById('mcp-f-url').value || '').trim();
        const headers = parseKeyVals(document.getElementById('mcp-f-headers').value, ':');
        if (url) block.url = url;
        // Only stamp "type" for the SSE override; http is the default and needs none.
        if (currentTransport === 'sse') block.type = 'sse';
        if (Object.keys(headers).length) block.headers = headers;
    }

    return { name, block };
}

function updatePreview() {
    const pre = document.getElementById('mcp-preview');
    if (!pre) return;
    const { name, block } = buildBlock();
    const key = name || '<name>';
    // Show just the single named block, the way you'd paste it under mcpServers.
    const snippet = { [key]: block };
    pre.textContent = JSON.stringify(snippet, null, 2);
}

function validBlock() {
    const { name, block } = buildBlock();
    if (!name) return { ok: false, msg: 'name is required' };
    if (currentTransport === 'stdio' && !block.command) return { ok: false, msg: 'command is required' };
    if (currentTransport !== 'stdio' && !block.url) return { ok: false, msg: 'url is required' };
    return { ok: true };
}

// Flatten the form into the McpServerIn shape the add endpoint expects.
function buildPayload() {
    const name = (document.getElementById('mcp-f-name').value || '').trim();
    if (currentTransport === 'stdio') {
        return {
            name,
            command: (document.getElementById('mcp-f-command').value || '').trim(),
            args: parseLines(document.getElementById('mcp-f-args').value),
            env: parseKeyVals(document.getElementById('mcp-f-env').value, '='),
        };
    }
    return {
        name,
        url: (document.getElementById('mcp-f-url').value || '').trim(),
        headers: parseKeyVals(document.getElementById('mcp-f-headers').value, ':'),
        transport: currentTransport === 'sse' ? 'sse' : null,
    };
}

function clearForm() {
    ['mcp-f-name', 'mcp-f-command', 'mcp-f-args', 'mcp-f-env', 'mcp-f-url', 'mcp-f-headers']
        .forEach((id) => { const el = document.getElementById(id); if (el) el.value = ''; });
}

async function submitServer() {
    const v = validBlock();
    if (!v.ok) { showToast(v.msg); return; }

    const submitBtn = document.getElementById('btn-mcp-submit');
    if (submitBtn) submitBtn.disabled = true;
    try {
        const r = await fetch(`${API}/mcp/servers`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(buildPayload()),
        });
        if (!r.ok) {
            let msg = 'failed to add server';
            try { const e = await r.json(); if (e.detail) msg = e.detail; } catch { /* ignore */ }
            showToast(msg);
            return;
        }
        const data = await r.json();
        renderServers(data.servers || []);
        clearForm();
        updatePreview();
        closeModal();
        showToast('server added — tools apply next turn');
    } catch {
        showToast('failed to add server');
    } finally {
        if (submitBtn) submitBtn.disabled = false;
    }
}

// ── modal open/close ──────────────────────────────────────────────────────────

function openModal() {
    const modal = document.getElementById('mcp-modal');
    if (!modal) return;
    modal.classList.add('active');
    document.body.style.overflow = 'hidden';
    updatePreview();
}

function closeModal() {
    const modal = document.getElementById('mcp-modal');
    if (!modal) return;
    modal.classList.remove('active');
    document.body.style.overflow = '';
}

function setTransport(transport) {
    currentTransport = transport;
    document.querySelectorAll('.mcp-tab').forEach((t) => {
        t.classList.toggle('active', t.getAttribute('data-transport') === transport);
    });
    const stdioGroup = document.querySelector('.mcp-group[data-group="stdio"]');
    const remoteGroup = document.querySelector('.mcp-group[data-group="remote"]');
    if (stdioGroup) stdioGroup.style.display = transport === 'stdio' ? '' : 'none';
    if (remoteGroup) remoteGroup.style.display = transport === 'stdio' ? 'none' : '';
    updatePreview();
}

// ── wiring ──────────────────────────────────────────────────────────────────

export function bindMcpPanel() {
    const reloadBtn = document.getElementById('btn-mcp-reload');
    if (reloadBtn) reloadBtn.addEventListener('click', reloadServers);

    const addBtn = document.getElementById('btn-mcp-add');
    if (addBtn) addBtn.addEventListener('click', openModal);

    const closeBtn = document.getElementById('close-mcp-modal');
    if (closeBtn) closeBtn.addEventListener('click', closeModal);

    const modal = document.getElementById('mcp-modal');
    if (modal) {
        modal.addEventListener('click', (e) => { if (e.target === modal) closeModal(); });
    }

    document.querySelectorAll('.mcp-tab').forEach((tab) => {
        tab.addEventListener('click', () => setTransport(tab.getAttribute('data-transport')));
    });

    // Live-preview as the user types.
    ['mcp-f-name', 'mcp-f-command', 'mcp-f-args', 'mcp-f-env', 'mcp-f-url', 'mcp-f-headers']
        .forEach((id) => {
            const el = document.getElementById(id);
            if (el) el.addEventListener('input', updatePreview);
        });

    const submitBtn = document.getElementById('btn-mcp-submit');
    if (submitBtn) submitBtn.addEventListener('click', submitServer);

    const addForm = document.getElementById('mcp-add-form');
    if (addForm) addForm.addEventListener('submit', (e) => { e.preventDefault(); submitServer(); });

    // Initial population.
    loadServers();
}
