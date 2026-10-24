import { API } from '../constants.js';
import { showToast } from '../utils/dom.js';
import { respondToPendingEdit, currentConversationId } from '../api/chat.js';

// ─── Single-edit pending state ────────────────────────────────────────────
//
// Break-and-resume model: at most ONE edit is pending approval per conversation
// at a time (the backend gates the turn on the first confirm-required tool
// call). The pending edit is durable server-side; the client mirrors just
// enough to render the modal and drive /respond. `pendingEdit` is the currently
// displayed pending edit, or null when nothing is pending.
//
// Shape (from the `confirm_required` SSE and GET /pending_edit):
//   { toolCallId, conversation_id, tool, edit:{path,action,anchor,summary},
//     preview:{ ok, old_content, new_content, target_path } | { ok:false, error } }
let pendingEdit = null;

/** True when an edit is pending approval for the open conversation. */
export function hasPendingEdit() {
    return pendingEdit !== null;
}

// ─── Auto-approve (frontend-only, edits-only, per-session) ──────────────────
//
// Pure client-side convenience: when on, a proposed edit whose preview is
// applicable is approved automatically instead of prompting. State lives on the
// composer chip (#chip-auto-approve-toggle) `.active` class — not persisted, so
// it defaults off every session, and it's per-chat since the user can flip it
// anytime. The backend doesn't know or care whether an approval was manual or
// auto. Deliberately scoped to EDITS only; it must not auto-approve any future
// confirm-required tool (e.g. sandbox commands), which don't flow through
// showPendingEdit's edit preview.
function isAutoApproveOn() {
    const chip = document.getElementById('chip-auto-approve-toggle');
    return !!chip && chip.classList.contains('active');
}

/**
 * If auto-approve is on and there's a currently-pending, cleanly-resolvable
 * edit, approve it now. Called when the user flips the chip on while an edit is
 * already waiting (so toggling on doesn't require a new proposal to take
 * effect). Exported for main.js to call from the chip handler.
 */
export function maybeAutoApprovePending() {
    if (isAutoApproveOn() && pendingEdit && pendingEdit.preview && pendingEdit.preview.ok) {
        resolvePending('approve');
    }
}

export function initEditPanel() {
    const panel = document.getElementById('edit-panel');
    if (!panel) return;

    const closeBtn = document.getElementById('close-edit-panel');
    if (closeBtn) {
        // Closing only hides the panel — the pending edit stays pending
        // server-side and the modal re-opens on reload or when re-fetched.
        closeBtn.addEventListener('click', () => panel.classList.remove('open'));
    }

    // Resizer logic
    const resizer = document.getElementById('edit-resizer');
    let isResizing = false;

    if (resizer) {
        resizer.addEventListener('mousedown', () => {
            isResizing = true;
            document.body.style.cursor = 'ew-resize';
            document.body.style.userSelect = 'none';
        });

        window.addEventListener('mousemove', (e) => {
            if (!isResizing) return;
            const newWidth = window.innerWidth - e.clientX;
            if (newWidth > 300 && newWidth < window.innerWidth - 100) {
                panel.style.width = newWidth + 'px';
            }
        });

        window.addEventListener('mouseup', () => {
            if (isResizing) {
                isResizing = false;
                document.body.style.cursor = '';
                document.body.style.userSelect = '';
            }
        });
    }

    // Approve / Reject for the single pending edit
    const approveBtn = document.getElementById('btn-approve-edit');
    const rejectBtn = document.getElementById('btn-reject-edit');
    if (approveBtn) approveBtn.addEventListener('click', () => resolvePending('approve'));
    if (rejectBtn) rejectBtn.addEventListener('click', () => resolvePending('reject'));

    // Restore any pending edit for the already-open conversation on page load.
    loadPendingEdit(currentConversationId);
}

// ─── Render ────────────────────────────────────────────────────────────────

/**
 * Handle the `confirm_required` SSE event: a single edit + preview gating the
 * turn. Stores it as the pending edit and renders the modal.
 */
export function handleConfirmRequired(data) {
    showPendingEdit(data);
    showToast(`Edit proposed: ${data.edit?.path || ''}`);
}

/**
 * Render the approval modal from a single pending-edit payload (from either the
 * `confirm_required` SSE or GET /pending_edit). If preview.ok is false, shows
 * the error, renders no diff and disables Approve (Reject stays enabled).
 */
function showPendingEdit(data) {
    pendingEdit = data;

    // Fresh edit → clear any stale reason text from a previous rejection.
    const reasonEl = document.getElementById('edit-reject-reason');
    if (reasonEl) reasonEl.value = '';

    const panel = document.getElementById('edit-panel');
    const edit = data.edit || {};
    const preview = data.preview || { ok: false, error: 'No preview available.' };

    // Header: action badge + path
    const badge = document.getElementById('edit-action-badge');
    if (badge) {
        const action = edit.action || '';
        badge.className = 'badge action-badge action-' + action;
        badge.textContent = (action || 'edit').replace(/_/g, ' ').toUpperCase();
    }
    const pathEl = document.getElementById('edit-path');
    if (pathEl) pathEl.textContent = edit.path || '';

    const summaryEl = document.getElementById('edit-summary-text');
    if (summaryEl) summaryEl.textContent = edit.summary || '';

    // Anchor context (hidden for create / empty anchor)
    const anchorEl = document.getElementById('edit-anchor');
    if (anchorEl) {
        if (edit.action !== 'create' && edit.anchor) {
            anchorEl.style.display = '';
            anchorEl.innerHTML = `<span class="anchor-label">anchor:</span> <code>${escapeHtml(truncate(edit.anchor, 120))}</code>`;
        } else {
            anchorEl.style.display = 'none';
            anchorEl.innerHTML = '';
        }
    }

    const errorEl = document.getElementById('edit-error');
    const diffEl = document.getElementById('edit-diff');
    const approveBtn = document.getElementById('btn-approve-edit');
    const oldCode = document.getElementById('edit-diff-old');
    const newCode = document.getElementById('edit-diff-new');

    if (preview.ok) {
        // Stale-anchor error cleared; render the diff.
        if (errorEl) { errorEl.style.display = 'none'; errorEl.textContent = ''; }
        if (diffEl) diffEl.style.display = '';
        if (approveBtn) approveBtn.disabled = false;
        if (oldCode && newCode) {
            renderDiff(preview.old_content || '', preview.new_content || '', oldCode, newCode);
            syncDiffScroll(oldCode, newCode);
        }
    } else {
        // Resolve failed now (e.g. file changed, anchor no longer unique): show
        // the error, render no diff, disable Approve but keep Reject enabled.
        if (errorEl) {
            errorEl.style.display = '';
            errorEl.textContent = preview.error || 'This edit can no longer be applied. Reject it and let the model re-propose.';
        }
        if (diffEl) diffEl.style.display = 'none';
        if (oldCode) oldCode.innerHTML = '';
        if (newCode) newCode.innerHTML = '';
        if (approveBtn) approveBtn.disabled = true;
    }

    // Re-enable both buttons' processing state (Approve may be disabled above).
    const rejectBtn = document.getElementById('btn-reject-edit');
    if (rejectBtn) rejectBtn.disabled = false;

    if (panel) panel.classList.add('open');

    // Lock the composer while an edit is pending.
    setInputPending(true);

    // Auto-approve: only for a cleanly-resolvable edit. A failed preview still
    // shows the modal so the user sees why (and the backend already gave the
    // model MAX_INVALID_EDIT_RETRIES attempts before surfacing it here).
    if (isAutoApproveOn() && preview.ok) {
        resolvePending('approve');
    }
}

// ─── Resolve (approve / reject) ──────────────────────────────────────────────

async function resolvePending(decision) {
    if (!pendingEdit) return;

    const { toolCallId } = pendingEdit;
    const conversationId = pendingEdit.conversation_id || currentConversationId;

    // On reject, capture the optional reason the user typed. The model sees it
    // as part of the tool result so it can adjust rather than blindly retry.
    let reason = null;
    if (decision === 'reject') {
        const reasonEl = document.getElementById('edit-reject-reason');
        reason = reasonEl ? reasonEl.value.trim() || null : null;
    }

    const approveBtn = document.getElementById('btn-approve-edit');
    const rejectBtn = document.getElementById('btn-reject-edit');
    if (approveBtn) approveBtn.disabled = true;
    if (rejectBtn) rejectBtn.disabled = true;

    // Surface the decision (and reason, if any) so there's a visible record of
    // what was sent to the model.
    if (decision === 'reject') {
        showToast(reason ? `Edit rejected: ${truncate(reason, 80)}` : 'Edit rejected');
    } else {
        showToast('Edit approved');
    }

    // Clear the pending state and close the modal BEFORE resuming the stream so
    // the input unlocks and the resumed turn renders cleanly. The resumed turn
    // may itself emit a fresh confirm_required, which re-opens the modal.
    clearPendingEdit();

    const started = await respondToPendingEdit(conversationId, toolCallId, decision, reason);

    if (!started) {
        // 409 or busy: the edit may still be pending. Re-fetch the durable
        // state so the modal reflects reality rather than silently vanishing.
        await loadPendingEdit(conversationId);
    }
}

// ─── Restore on chat open / page load ────────────────────────────────────────

/**
 * Fetch the durable pending edit for a conversation and render it, or clear the
 * modal if there is none. Safe to call with a null/empty conversationId (no-op
 * that clears any stale modal). Used on chat open, page reload and after a
 * server restart — the pending state lives in the DB.
 */
export async function loadPendingEdit(conversationId) {
    if (!conversationId) {
        clearPendingEdit();
        return;
    }
    try {
        const res = await fetch(`${API}/pending_edit/${conversationId}`);
        if (!res.ok) { clearPendingEdit(); return; }
        // Contract: body is `null` (HTTP 200) when nothing is pending.
        const data = await res.json();
        if (data && data.toolCallId) {
            showPendingEdit(data);
        } else {
            clearPendingEdit();
        }
    } catch (e) {
        console.warn('Failed to load pending edit:', e);
        clearPendingEdit();
    }
}

/** Drop any pending edit, hide the modal and unlock the composer. */
export function clearPendingEdit() {
    pendingEdit = null;
    const panel = document.getElementById('edit-panel');
    if (panel) panel.classList.remove('open');
    setInputPending(false);
}

// ─── Composer lock ───────────────────────────────────────────────────────────

/**
 * Disable/enable the message input + send button while an edit is pending. The
 * backend also rejects new messages with a 409 backstop; this just keeps the UI
 * honest so that never fires in normal use.
 */
function setInputPending(pending) {
    const input = document.getElementById('user-input');
    const sendBtn = document.getElementById('send-btn');
    if (input) {
        input.disabled = pending;
        input.placeholder = pending
            ? 'Approve or reject the pending edit to continue...'
            : 'ask something...';
    }
    // Don't fight the streaming lifecycle: only toggle the send button when not
    // mid-stream. chat.js owns send-btn.disabled during an active stream.
    if (sendBtn && pending) sendBtn.disabled = true;
    else if (sendBtn && !pending) sendBtn.disabled = false;
}

// ─── Diff rendering ───────────────────────────────────────────────────────

function syncDiffScroll(oldCode, newCode) {
    let isSyncingLeft = false;
    let isSyncingRight = false;
    oldCode.onscroll = () => {
        if (!isSyncingLeft) {
            isSyncingRight = true;
            newCode.scrollTop = oldCode.scrollTop;
            newCode.scrollLeft = oldCode.scrollLeft;
        }
        isSyncingLeft = false;
    };
    newCode.onscroll = () => {
        if (!isSyncingRight) {
            isSyncingLeft = true;
            oldCode.scrollTop = newCode.scrollTop;
            oldCode.scrollLeft = newCode.scrollLeft;
        }
        isSyncingRight = false;
    };
}

function renderDiff(oldText, newText, oldContainer, newContainer) {
    oldContainer.innerHTML = '';
    newContainer.innerHTML = '';

    if (typeof Diff === 'undefined') {
        oldContainer.textContent = oldText;
        newContainer.textContent = newText;
        return;
    }

    const diff = Diff.diffLines(oldText, newText);
    let i = 0;
    while (i < diff.length) {
        const part = diff[i];
        if (part.removed && i + 1 < diff.length && diff[i + 1].added) {
            const removedLines = splitLines(part.value);
            const addedLines = splitLines(diff[i + 1].value);
            const pairCount = Math.min(removedLines.length, addedLines.length);
            for (let p = 0; p < pairCount; p++) {
                renderIntraLineDiff(removedLines[p], addedLines[p], oldContainer, newContainer);
            }
            for (let p = pairCount; p < removedLines.length; p++) {
                appendPlainLine(oldContainer, removedLines[p], 'del');
                appendPlainLine(newContainer, '', 'empty');
            }
            for (let p = pairCount; p < addedLines.length; p++) {
                appendPlainLine(oldContainer, '', 'empty');
                appendPlainLine(newContainer, addedLines[p], 'add');
            }
            i += 2;
        } else if (part.added) {
            appendLines(newContainer, part.value, 'add');
            appendLines(oldContainer, '\n'.repeat(part.count), 'empty');
            i++;
        } else if (part.removed) {
            appendLines(oldContainer, part.value, 'del');
            appendLines(newContainer, '\n'.repeat(part.count), 'empty');
            i++;
        } else {
            appendLines(oldContainer, part.value, 'unchanged');
            appendLines(newContainer, part.value, 'unchanged');
            i++;
        }
    }
}

function renderIntraLineDiff(oldLine, newLine, oldContainer, newContainer) {
    const wordDiff = Diff.diffWords(oldLine, newLine);
    const oldDiv = document.createElement('div');
    const newDiv = document.createElement('div');
    oldDiv.className = 'diff-line del';
    newDiv.className = 'diff-line add';

    wordDiff.forEach(token => {
        if (token.removed) {
            const span = document.createElement('span');
            span.className = 'intra-del';
            span.textContent = token.value;
            oldDiv.appendChild(span);
        } else if (token.added) {
            const span = document.createElement('span');
            span.className = 'intra-add';
            span.textContent = token.value;
            newDiv.appendChild(span);
        } else {
            oldDiv.appendChild(document.createTextNode(token.value));
            newDiv.appendChild(document.createTextNode(token.value));
        }
    });

    oldContainer.appendChild(oldDiv);
    newContainer.appendChild(newDiv);
}

function splitLines(text) {
    const lines = text.split('\n');
    if (lines[lines.length - 1] === '') lines.pop();
    return lines;
}

function appendPlainLine(container, text, type) {
    const div = document.createElement('div');
    div.className = `diff-line ${type}`;
    div.textContent = text || ' ';
    container.appendChild(div);
}

function appendLines(container, text, type) {
    if (!text) return;
    splitLines(text).forEach(line => appendPlainLine(container, line, type));
}

// ─── Helpers ──────────────────────────────────────────────────────────────

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function truncate(text, maxLen) {
    if (text.length <= maxLen) return text;
    return text.slice(0, maxLen) + '\u2026';
}
