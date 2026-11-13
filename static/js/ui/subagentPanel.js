import { API } from '../constants.js';

/**
 * Subagent activity panel.
 *
 * Live: handleSubagentEvent() is dispatched from chat.js handleStreamEvent for
 * the `subagent_start | subagent_tool | subagent_tool_result | subagent_digest
 * | subagent_failed` SSE events (each carries a `tool_call_id` keying its run).
 * The panel slides out automatically when a run starts; the user can hide it
 * before the run finishes and it stays running underneath.
 *
 * Restore: loadSubagentRuns(conversationId) fetches GET /subagent_runs/<id> on
 * chat open and renders past runs (collapsed), mirroring editPanel.loadPendingEdit.
 *
 * Scope: actives + past runs of the CURRENT chat only (the endpoint is keyed by
 * conversation). A fresh/other chat resets the panel.
 */

const panel = () => document.getElementById('subagent-panel');
const listEl = () => document.getElementById('subagent-list');
const badgeEl = () => document.getElementById('subagent-badge');
const btnEl = () => document.getElementById('subagent-btn');

// tool_call_id -> { el, startedAt, status, timerId }
const runs = new Map();
let autoOpenedThisBatch = false;
// Pending auto-hide timer, so a new run (or manual toggle) can cancel it.
let autoHideTimerId = null;
const AUTO_HIDE_DELAY_MS = 1500;

export function initSubagentPanel() {
    const close = document.getElementById('close-subagent-panel');
    const btn = btnEl();
    const p = panel();

    if (btn) btn.addEventListener('click', togglePanel);
    // Hiding only closes the panel — running subagents keep going underneath.
    if (close) close.addEventListener('click', hidePanel);

    // Resizer (same interaction as the edit panel).
    const resizer = document.getElementById('subagent-resizer');
    let isResizing = false;
    if (resizer && p) {
        resizer.addEventListener('mousedown', () => {
            isResizing = true;
            document.body.style.cursor = 'ew-resize';
            document.body.style.userSelect = 'none';
        });
        window.addEventListener('mousemove', (e) => {
            if (!isResizing) return;
            const newWidth = window.innerWidth - e.clientX;
            if (newWidth > 320 && newWidth < window.innerWidth - 100) {
                p.style.width = newWidth + 'px';
            }
        });
        window.addEventListener('mouseup', () => {
            if (!isResizing) return;
            isResizing = false;
            document.body.style.cursor = '';
            document.body.style.userSelect = '';
        });
    }
}

function togglePanel() {
    const p = panel();
    if (!p) return;
    // Any manual interaction cancels a pending auto-hide — the user is in charge.
    cancelAutoHide();
    p.classList.toggle('open');
}

function showPanel() {
    const p = panel();
    if (p) p.classList.add('open');
}

function hidePanel() {
    cancelAutoHide();
    const p = panel();
    if (p) p.classList.remove('open');
}

function cancelAutoHide() {
    if (autoHideTimerId) { clearTimeout(autoHideTimerId); autoHideTimerId = null; }
}

/**
 * Schedule the panel to slide shut after a short delay once a run finishes.
 * Only hides when no subagent is still running, and re-checks at fire time so a
 * fresh run started during the delay keeps the panel open.
 */
function scheduleAutoHide() {
    cancelAutoHide();
    if (activeCount() > 0) return;
    autoHideTimerId = setTimeout(() => {
        autoHideTimerId = null;
        if (activeCount() === 0) {
            const p = panel();
            if (p) p.classList.remove('open');
        }
    }, AUTO_HIDE_DELAY_MS);
}

// ─── Badge / button state ────────────────────────────────────────────────────

function activeCount() {
    let n = 0;
    for (const r of runs.values()) if (r.status === 'running') n++;
    return n;
}

function refreshBadge() {
    const badge = badgeEl();
    const btn = btnEl();
    const n = activeCount();
    if (badge) {
        if (n > 0) {
            badge.textContent = String(n);
            badge.hidden = false;
        } else {
            badge.hidden = true;
        }
    }
    if (btn) btn.classList.toggle('running', n > 0);
}

// ─── Live SSE handling ───────────────────────────────────────────────────────

/**
 * Dispatch a subagent_* SSE event. `data` is the parsed payload object (already
 * JSON-parsed by chat.js), always carrying `tool_call_id`.
 */
export function handleSubagentEvent(event, data) {
    const payload = (typeof data === 'string') ? safeParse(data) : (data || {});
    const id = payload.tool_call_id || payload.agent || 'run';

    if (event === 'subagent_start') {
        clearEmpty();
        cancelAutoHide();
        // New live batch: auto-slide-out once so the running subagent is visible.
        if (!autoOpenedThisBatch) {
            showPanel();
            autoOpenedThisBatch = true;
        }
        createRun(id, payload.agent, payload.task);
        refreshBadge();
    } else if (event === 'subagent_token') {
        appendToken(id, payload.token || '');
    } else if (event === 'subagent_tool') {
        // The round resolved into a tool call → the text streamed so far was
        // pre-tool scratch, not an answer. Drop it; the step pill tells the story.
        clearLive(id);
        addStep(id, `🔎 ${payload.tool}  ${argPreview(payload.args)}`);
    } else if (event === 'subagent_tool_result') {
        addStep(id, `✓ ${payload.tool} → ${preview(payload.result, 80)}`, 'result');
    } else if (event === 'subagent_digest') {
        // The accumulated live text IS the digest; finishRun renders it into the
        // digest box, so clear the transient live line.
        clearLive(id);
        finishRun(id, 'done', payload.digest);
        refreshBadge();
    } else if (event === 'subagent_failed') {
        finishRun(id, 'failed', payload.reason || 'subagent failed');
        refreshBadge();
    }
}

/** Reset the auto-open latch so the NEXT turn's first subagent slides out again. */
export function resetSubagentBatch() {
    autoOpenedThisBatch = false;
}

// ─── Card builders ───────────────────────────────────────────────────────────

function createRun(id, agent, task) {
    if (runs.get(id)) return runs.get(id);
    const card = buildCard({
        agent: agent || 'subagent',
        task: task || '',
        status: 'running',
    });
    listEl().prepend(card.root); // newest on top
    const startedAt = Date.now();
    const timerId = setInterval(() => {
        card.timer.textContent = fmtElapsed(Date.now() - startedAt);
    }, 500);
    const rec = { el: card, startedAt, status: 'running', timerId };
    runs.set(id, rec);
    // Running cards start expanded so steps stream into view.
    card.root.classList.add('expanded');
    return rec;
}

function addStep(id, text, kind = '') {
    const rec = runs.get(id);
    if (!rec) return;
    const step = document.createElement('div');
    step.className = 'subagent-step' + (kind ? ` ${kind}` : '');
    step.textContent = text;
    rec.el.steps.appendChild(step);
    rec.el.body.scrollTop = rec.el.body.scrollHeight;
}

/** Append a streamed content token to the card's live "typing" line. */
function appendToken(id, token) {
    const rec = runs.get(id);
    if (!rec || !token) return;
    rec.live = (rec.live || '') + token;
    rec.el.live.hidden = false;
    rec.el.live.textContent = rec.live;
    rec.el.body.scrollTop = rec.el.body.scrollHeight;
}

/** Clear the transient live line (round resolved into a tool call or digest). */
function clearLive(id) {
    const rec = runs.get(id);
    if (!rec) return;
    rec.live = '';
    rec.el.live.textContent = '';
    rec.el.live.hidden = true;
}

function finishRun(id, status, digestText) {
    const rec = runs.get(id);
    if (!rec) return;
    if (rec.timerId) { clearInterval(rec.timerId); rec.timerId = null; }
    rec.status = status;
    const root = rec.el.root;
    root.classList.remove('running');
    root.classList.add(status === 'failed' ? 'failed' : 'done');

    if (digestText) {
        rec.el.digestWrap.hidden = false;
        rec.el.digest.textContent = digestText;
        rec.el.preview.textContent = preview(digestText, 90);
    }
    // Collapse finished runs so the panel stays scannable; click to reopen.
    root.classList.remove('expanded');

    scheduleAutoHide();
}

/** Build the DOM for one run card. Returns handles used to update it live. */
function buildCard({ agent, task, status }) {
    const root = document.createElement('div');
    root.className = `subagent-card ${status}`;

    const head = document.createElement('div');
    head.className = 'subagent-card-head';

    const dot = document.createElement('span');
    dot.className = 'subagent-status-dot';

    const agentEl = document.createElement('span');
    agentEl.className = 'subagent-agent';
    agentEl.textContent = agent;

    const timer = document.createElement('span');
    timer.className = 'subagent-timer';
    timer.textContent = status === 'running' ? '0:00' : '';

    const caret = document.createElement('span');
    caret.className = 'subagent-caret';
    caret.textContent = '▸';

    head.append(dot, agentEl, timer, caret);
    head.addEventListener('click', () => root.classList.toggle('expanded'));

    const taskEl = document.createElement('div');
    taskEl.className = 'subagent-task';
    taskEl.textContent = task;

    const preview = document.createElement('div');
    preview.className = 'subagent-preview';

    const body = document.createElement('div');
    body.className = 'subagent-body';
    const steps = document.createElement('div');
    steps.className = 'subagent-steps';
    body.appendChild(steps);

    // Live "typing" line: streamed content tokens for the current round, shown
    // while granite generates and cleared when the round resolves.
    const live = document.createElement('div');
    live.className = 'subagent-live';
    live.hidden = true;
    body.appendChild(live);

    const digestWrap = document.createElement('div');
    digestWrap.className = 'subagent-digest';
    digestWrap.hidden = true;
    const digestLabel = document.createElement('span');
    digestLabel.className = 'subagent-digest-label';
    digestLabel.textContent = 'digest';
    const digest = document.createElement('div');
    digestWrap.append(digestLabel, digest);
    body.appendChild(digestWrap);

    root.append(head, taskEl, preview, body);
    return { root, dot, timer, task: taskEl, preview, body, steps, live, digest, digestWrap };
}

// ─── Restore on chat open ────────────────────────────────────────────────────

/**
 * Fetch and render persisted subagent runs for a conversation (past runs,
 * collapsed). Clears the panel for a null/unknown conversation. Does NOT open
 * the panel — reopen defaults to hidden (user clicks the button to check).
 */
export async function loadSubagentRuns(conversationId) {
    resetRuns();
    if (!conversationId) { renderEmpty(); return; }
    try {
        const res = await fetch(`${API}/subagent_runs/${conversationId}`);
        if (!res.ok) { renderEmpty(); return; }
        const data = await res.json();
        const items = (data && data.runs) || [];
        if (!items.length) { renderEmpty(); return; }
        clearEmpty();
        items.forEach(renderPastRun);
    } catch (e) {
        console.warn('Failed to load subagent runs:', e);
        renderEmpty();
    }
}

function renderPastRun(run) {
    const status = run.status === 'failed' ? 'failed' : 'done';
    const card = buildCard({
        agent: run.agent_name || 'subagent',
        task: run.task || '',
        status,
    });
    card.root.classList.add('past');
    card.timer.textContent = '';

    // Replay transcript steps if present.
    const transcript = Array.isArray(run.transcript) ? run.transcript : [];
    transcript.forEach(entry => {
        if (entry && entry.role === 'tool') {
            const step = document.createElement('div');
            step.className = 'subagent-step';
            step.textContent = `🔎 ${entry.tool || 'tool'}  ${argPreview(entry.args)}`;
            card.steps.appendChild(step);
            const r = document.createElement('div');
            r.className = 'subagent-step result';
            r.textContent = `✓ → ${preview(entry.content, 80)}`;
            card.steps.appendChild(r);
        }
    });

    if (run.digest) {
        card.digestWrap.hidden = false;
        card.digest.textContent = run.digest;
        card.preview.textContent = preview(run.digest, 90);
    }
    listEl().prepend(card.root);
}

// ─── Housekeeping ────────────────────────────────────────────────────────────

function resetRuns() {
    for (const r of runs.values()) if (r.timerId) clearInterval(r.timerId);
    runs.clear();
    autoOpenedThisBatch = false;
    cancelAutoHide();
    const l = listEl();
    if (l) l.innerHTML = '';
    refreshBadge();
}

function renderEmpty() {
    const l = listEl();
    if (l) l.innerHTML = '<div class="subagent-empty">No subagent runs in this chat yet.</div>';
}

function clearEmpty() {
    const l = listEl();
    if (!l) return;
    const empty = l.querySelector('.subagent-empty');
    if (empty) empty.remove();
}

// ─── Utils ───────────────────────────────────────────────────────────────────

function safeParse(s) { try { return JSON.parse(s); } catch { return {}; } }

function preview(v, n) {
    const s = (v == null ? '' : String(v)).replace(/\s+/g, ' ').trim();
    return s.length > n ? s.slice(0, n) + '…' : s;
}

function argPreview(args) {
    if (!args) return '';
    const q = args.query || args.url || args.task || args.path || '';
    return q ? `"${preview(q, 60)}"` : preview(JSON.stringify(args), 60);
}

function fmtElapsed(ms) {
    const s = Math.floor(ms / 1000);
    const m = Math.floor(s / 60);
    return `${m}:${String(s % 60).padStart(2, '0')}`;
}
