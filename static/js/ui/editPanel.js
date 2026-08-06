import { API } from '../constants.js';
import { showToast } from '../utils/dom.js';

// All pending batches keyed by toolCallId
let pendingBatches = {};

export function initEditPanel() {
    const panel = document.getElementById('edit-panel');
    const closeBtn = document.getElementById('close-edit-panel');

    if (!panel) return;

    closeBtn.addEventListener('click', () => panel.classList.remove('open'));

    // Resizer logic
    const resizer = document.getElementById('edit-resizer');
    let isResizing = false;

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

    // Bulk actions
    document.getElementById('btn-approve-all').addEventListener('click', () => resolveAll('approve'));
    document.getElementById('btn-reject-all').addEventListener('click', () => resolveAll('reject'));

    // Restore pending edits from server on page load
    loadPendingEdits();
}

export function handleProposeEdit(data) {
    // data: {toolCallId, edits: [{index, path, action, anchor, content, old_content, new_content, target_path}], summary}
    pendingBatches[data.toolCallId] = data;

    const panel = document.getElementById('edit-panel');
    const cardsContainer = document.getElementById('edit-cards');

    // Render cards for this batch (append — don't clear, other batches may be pending)
    for (const edit of data.edits) {
        const card = createEditCard(data.toolCallId, edit, data.summary);
        cardsContainer.appendChild(card);
    }

    panel.classList.add('open');
    updateSummary();
    showToast(`Edit proposed: ${data.edits.length} edit(s)`);
}

function updateSummary() {
    const summaryEl = document.getElementById('edit-summary');
    const cardCount = document.querySelectorAll('#edit-cards .edit-card').length;
    summaryEl.textContent = cardCount > 0
        ? `${cardCount} pending edit${cardCount !== 1 ? 's' : ''}`
        : 'No pending edits';
}

function createEditCard(toolCallId, edit, batchSummary) {
    const card = document.createElement('div');
    card.className = 'edit-card';
    card.dataset.toolCallId = toolCallId;
    card.dataset.index = edit.index;

    const isNew = edit.action === 'create';

    // Header row
    const header = document.createElement('div');
    header.className = 'edit-card-header';

    const actionBadge = document.createElement('span');
    actionBadge.className = 'badge action-badge action-' + edit.action;
    actionBadge.textContent = edit.action.replace('_', ' ').toUpperCase();

    const pathEl = document.createElement('span');
    pathEl.className = 'edit-card-path';
    pathEl.textContent = edit.path;

    // Per-card action buttons
    const actions = document.createElement('div');
    actions.className = 'edit-card-actions';

    const approveBtn = document.createElement('button');
    approveBtn.className = 'card-btn-approve';
    approveBtn.textContent = 'Approve';
    approveBtn.addEventListener('click', () => resolveOne(card, toolCallId, edit.index, 'approve'));

    const rejectBtn = document.createElement('button');
    rejectBtn.className = 'card-btn-reject';
    rejectBtn.textContent = 'Reject';
    rejectBtn.addEventListener('click', () => resolveOne(card, toolCallId, edit.index, 'reject'));

    actions.appendChild(approveBtn);
    actions.appendChild(rejectBtn);

    header.appendChild(actionBadge);
    header.appendChild(pathEl);
    header.appendChild(actions);

    // Anchor context
    if (!isNew && edit.anchor) {
        const anchorEl = document.createElement('div');
        anchorEl.className = 'edit-card-anchor';
        anchorEl.innerHTML = `<span class="anchor-label">anchor:</span> <code>${escapeHtml(truncate(edit.anchor, 120))}</code>`;
        card.appendChild(header);
        card.appendChild(anchorEl);
    } else {
        card.appendChild(header);
    }

    // Diff view
    const diffView = document.createElement('div');
    diffView.className = 'edit-card-diff';

    const oldPane = document.createElement('div');
    oldPane.className = 'diff-pane';
    const oldHeader = document.createElement('div');
    oldHeader.className = 'diff-pane-header';
    oldHeader.textContent = 'Before';
    const oldCode = document.createElement('div');
    oldCode.className = 'diff-code';
    oldPane.appendChild(oldHeader);
    oldPane.appendChild(oldCode);

    const newPane = document.createElement('div');
    newPane.className = 'diff-pane';
    const newHeader = document.createElement('div');
    newHeader.className = 'diff-pane-header';
    newHeader.textContent = 'After';
    const newCode = document.createElement('div');
    newCode.className = 'diff-code';
    newPane.appendChild(newHeader);
    newPane.appendChild(newCode);

    diffView.appendChild(oldPane);
    diffView.appendChild(newPane);

    renderDiff(edit.old_content || '', edit.new_content || '', oldCode, newCode);

    // Sync scrolling
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

    card.appendChild(diffView);
    return card;
}

async function resolveOne(card, toolCallId, editIndex, action) {
    // Disable buttons while processing
    const btns = card.querySelectorAll('button');
    btns.forEach(b => b.disabled = true);

    try {
        const res = await fetch(`${API}/resolve_edit`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                toolCallId,
                decisions: [{ index: editIndex, action }]
            })
        });

        if (res.ok) {
            // Animate out and remove
            card.classList.add(action === 'approve' ? 'resolved-approved' : 'resolved-rejected');
            setTimeout(() => {
                card.remove();
                updateSummary();
                closePanelIfEmpty();
            }, 300);

            showToast(`${action === 'approve' ? 'Approved' : 'Rejected'}: ${card.querySelector('.edit-card-path').textContent}`);
            addHistoryEntry(card.querySelector('.edit-card-path').textContent, action);
        } else {
            const data = await res.json();
            showToast(data.detail || 'Failed to resolve edit');
            btns.forEach(b => b.disabled = false);
        }
    } catch (e) {
        console.error(e);
        showToast('Error resolving edit');
        btns.forEach(b => b.disabled = false);
    }
}

async function resolveAll(action) {
    const cards = document.querySelectorAll('#edit-cards .edit-card');
    if (cards.length === 0) return;

    // Group by toolCallId
    const groups = {};
    cards.forEach(card => {
        const tcId = card.dataset.toolCallId;
        if (!groups[tcId]) groups[tcId] = [];
        groups[tcId].push(parseInt(card.dataset.index, 10));
    });

    // Fire one request per batch
    for (const [toolCallId, indices] of Object.entries(groups)) {
        try {
            const res = await fetch(`${API}/resolve_edit`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    toolCallId,
                    bulk_action: action === 'approve' ? 'approve_all' : 'reject_all'
                })
            });

            if (res.ok) {
                // Remove all cards for this batch
                cards.forEach(card => {
                    if (card.dataset.toolCallId === toolCallId) {
                        card.classList.add(action === 'approve' ? 'resolved-approved' : 'resolved-rejected');
                    }
                });
            }
        } catch (e) {
            console.error(e);
        }
    }

    setTimeout(() => {
        document.querySelectorAll('#edit-cards .edit-card').forEach(c => c.remove());
        updateSummary();
        closePanelIfEmpty();
    }, 300);

    showToast(`${action === 'approve' ? 'Approved' : 'Rejected'} all edits`);
    addHistoryEntry(`All (${cards.length} edits)`, action);
}

function closePanelIfEmpty() {
    const remaining = document.querySelectorAll('#edit-cards .edit-card').length;
    if (remaining === 0) {
        document.getElementById('edit-panel').classList.remove('open');
        pendingBatches = {};
    }
}

function addHistoryEntry(label, action) {
    const list = document.getElementById('edit-history-list');
    const div = document.createElement('div');
    div.className = `history-item ${action === 'approve' ? 'approved' : 'rejected'}`;
    div.innerHTML = `<strong>${escapeHtml(label)}</strong> <small>${action.toUpperCase()}</small>`;
    list.prepend(div);
}

// ─── Restore on page load ─────────────────────────────────────────────────

async function loadPendingEdits() {
    try {
        const res = await fetch(`${API}/pending_edits`);
        if (!res.ok) return;
        const batches = await res.json();
        if (!batches || batches.length === 0) return;

        // Re-render all pending batches
        for (const batch of batches) {
            handleProposeEdit(batch);
        }
    } catch (e) {
        console.warn('Failed to load pending edits:', e);
    }
}

// ─── Diff rendering ───────────────────────────────────────────────────────

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
