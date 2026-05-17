import { API } from '../constants.js';
import { showToast } from '../utils/dom.js';

let currentPendingEdit = null;

export function initEditPanel() {
    const panel = document.getElementById('edit-panel');
    const closeBtn = document.getElementById('close-edit-panel');
    const approveBtn = document.getElementById('btn-approve');
    const rejectBtn = document.getElementById('btn-reject');
    const reasonInput = document.getElementById('reject-reason');

    if (!panel) return;

    closeBtn.addEventListener('click', () => panel.classList.remove('open'));

    approveBtn.addEventListener('click', async () => {
        if (!currentPendingEdit) return;
        await resolveEdit('approve', '');
    });

    rejectBtn.addEventListener('click', async () => {
        if (!currentPendingEdit) return;
        const reason = reasonInput.value.trim();
        await resolveEdit('reject', reason);
    });

    // Resizer logic
    const resizer = document.getElementById('edit-resizer');
    let isResizing = false;
    
    resizer.addEventListener('mousedown', (e) => {
        isResizing = true;
        document.body.style.cursor = 'ew-resize';
        document.body.style.userSelect = 'none';
    });
    
    window.addEventListener('mousemove', (e) => {
        if (!isResizing) return;
        // Panel is on the right, so width is (window.innerWidth - e.clientX)
        const newWidth = window.innerWidth - e.clientX;
        // Enforce min and max widths
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

export function handleProposeEdit(data) {
    currentPendingEdit = data;
    
    const panel = document.getElementById('edit-panel');
    const badge = document.getElementById('edit-badge');
    const pathEl = document.getElementById('edit-path');
    const summaryEl = document.getElementById('edit-summary');
    const oldCode = document.getElementById('diff-old');
    const newCode = document.getElementById('diff-new');
    const reasonInput = document.getElementById('reject-reason');
    
    // reset UI
    reasonInput.value = '';
    
    const isNew = !data.old_content;
    badge.className = 'badge ' + (isNew ? 'new-file' : 'edit-file');
    badge.textContent = isNew ? 'NEW FILE' : 'EDIT';
    
    pathEl.textContent = data.path;
    summaryEl.textContent = data.summary;
    
    renderDiff(data.old_content || '', data.new_content || '', oldCode, newCode);
    
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

    panel.classList.add('open');
    showToast('New file edit proposed');
}

function renderDiff(oldText, newText, oldContainer, newContainer) {
    oldContainer.innerHTML = '';
    newContainer.innerHTML = '';

    // If jsdiff is not loaded, fallback to simple text
    if (typeof Diff === 'undefined') {
        oldContainer.textContent = oldText;
        newContainer.textContent = newText;
        return;
    }

    // Collect the line-level diff chunks, then group consecutive removed/added
    // pairs so we can do a secondary word-level diff on them.
    const diff = Diff.diffLines(oldText, newText);

    let i = 0;
    while (i < diff.length) {
        const part = diff[i];

        if (part.removed && i + 1 < diff.length && diff[i + 1].added) {
            // We have a removed block immediately followed by an added block.
            // Split both into individual lines and pair them up for word-level diffs.
            const removedLines = splitLines(part.value);
            const addedLines   = splitLines(diff[i + 1].value);
            const pairCount    = Math.min(removedLines.length, addedLines.length);

            // Paired lines — render with intra-line word diff
            for (let p = 0; p < pairCount; p++) {
                renderIntraLineDiff(removedLines[p], addedLines[p], oldContainer, newContainer);
            }

            // Surplus removed lines (more removed than added)
            for (let p = pairCount; p < removedLines.length; p++) {
                appendPlainLine(oldContainer, removedLines[p], 'del');
                appendPlainLine(newContainer, '', 'empty');
            }

            // Surplus added lines (more added than removed)
            for (let p = pairCount; p < addedLines.length; p++) {
                appendPlainLine(oldContainer, '', 'empty');
                appendPlainLine(newContainer, addedLines[p], 'add');
            }

            i += 2; // consume both the removed and the added chunk
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

/**
 * Renders a single paired old/new line with word-level highlights so only
 * the changed words are coloured instead of the entire line.
 */
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
            // Unchanged token — show in both sides as plain text
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
    const lines = splitLines(text);
    lines.forEach(line => {
        appendPlainLine(container, line, type);
    });
}

async function resolveEdit(action, reason) {
    try {
        const res = await fetch(`${API}/resolve_edit`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                toolCallId: currentPendingEdit.toolCallId,
                action,
                reason
            })
        });

        if (res.ok) {
            showToast(`Edit ${action === 'approve' ? 'approved' : 'rejected'}`);
            addHistoryItem(currentPendingEdit, action, reason);
            currentPendingEdit = null;
            document.getElementById('edit-panel').classList.remove('open');
        } else {
            const data = await res.json();
            showToast(data.detail || 'Failed to resolve edit');
        }
    } catch (e) {
        console.error(e);
        showToast('Error resolving edit');
    }
}

function addHistoryItem(edit, action, reason) {
    const list = document.getElementById('edit-history-list');
    const div = document.createElement('div');
    div.className = `history-item ${action === 'approve' ? 'approved' : 'rejected'}`;
    div.innerHTML = `
        <strong>${edit.path}</strong>: ${edit.summary}<br>
        <small>${action.toUpperCase()}${reason ? ' - ' + reason : ''}</small>
    `;
    list.prepend(div);
}
