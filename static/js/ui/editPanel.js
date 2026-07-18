import { API } from '../constants.js';
import { showToast } from '../utils/dom.js';

let currentPendingBatch = null;  // {toolCallId, files: [...], summary}

export function initEditPanel() {
    const panel = document.getElementById('edit-panel');
    const closeBtn = document.getElementById('close-edit-panel');
    const submitBtn = document.getElementById('btn-submit-batch');

    if (!panel) return;

    closeBtn.addEventListener('click', () => panel.classList.remove('open'));

    submitBtn.addEventListener('click', async () => {
        if (!currentPendingBatch) return;
        await submitBatchDecisions();
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

export function handleProposeEdit(data) {
    // data shape: {toolCallId, files: [{path, old_content, new_content, target_path}], summary}
    currentPendingBatch = data;

    const panel = document.getElementById('edit-panel');
    const summaryEl = document.getElementById('edit-summary');
    const cardsContainer = document.getElementById('edit-cards');

    // Reset
    cardsContainer.innerHTML = '';
    summaryEl.textContent = data.summary || '';

    // Render a card for each file
    for (const file of data.files) {
        const card = createFileCard(file);
        cardsContainer.appendChild(card);
    }

    panel.classList.add('open');
    showToast(`Edit proposed: ${data.files.length} file(s)`);
}

function createFileCard(file) {
    const card = document.createElement('div');
    card.className = 'edit-card';
    card.dataset.path = file.path;
    card.dataset.decision = 'approve'; // default to approve

    const isNew = !file.old_content;

    // Header
    const header = document.createElement('div');
    header.className = 'edit-card-header';

    const badge = document.createElement('span');
    badge.className = 'badge ' + (isNew ? 'new-file' : 'edit-file');
    badge.textContent = isNew ? 'NEW' : 'EDIT';

    const pathEl = document.createElement('span');
    pathEl.className = 'edit-card-path';
    pathEl.textContent = file.path;

    const toggleContainer = document.createElement('div');
    toggleContainer.className = 'edit-card-toggle';

    const approveBtn = document.createElement('button');
    approveBtn.className = 'toggle-approve active';
    approveBtn.textContent = 'Approve';
    approveBtn.addEventListener('click', () => {
        card.dataset.decision = 'approve';
        approveBtn.classList.add('active');
        rejectBtn.classList.remove('active');
        card.classList.remove('rejected');
    });

    const rejectBtn = document.createElement('button');
    rejectBtn.className = 'toggle-reject';
    rejectBtn.textContent = 'Reject';
    rejectBtn.addEventListener('click', () => {
        card.dataset.decision = 'reject';
        rejectBtn.classList.add('active');
        approveBtn.classList.remove('active');
        card.classList.add('rejected');
    });

    toggleContainer.appendChild(approveBtn);
    toggleContainer.appendChild(rejectBtn);

    header.appendChild(badge);
    header.appendChild(pathEl);
    header.appendChild(toggleContainer);

    // Diff view
    const diffView = document.createElement('div');
    diffView.className = 'edit-card-diff';

    const oldPane = document.createElement('div');
    oldPane.className = 'diff-pane';
    const oldHeader = document.createElement('div');
    oldHeader.className = 'diff-pane-header';
    oldHeader.textContent = 'Original';
    const oldCode = document.createElement('div');
    oldCode.className = 'diff-code';
    oldPane.appendChild(oldHeader);
    oldPane.appendChild(oldCode);

    const newPane = document.createElement('div');
    newPane.className = 'diff-pane';
    const newHeader = document.createElement('div');
    newHeader.className = 'diff-pane-header';
    newHeader.textContent = 'Proposed';
    const newCode = document.createElement('div');
    newCode.className = 'diff-code';
    newPane.appendChild(newHeader);
    newPane.appendChild(newCode);

    diffView.appendChild(oldPane);
    diffView.appendChild(newPane);

    renderDiff(file.old_content || '', file.new_content || '', oldCode, newCode);

    // Sync scrolling between panes
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

    card.appendChild(header);
    card.appendChild(diffView);

    return card;
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
            const addedLines   = splitLines(diff[i + 1].value);
            const pairCount    = Math.min(removedLines.length, addedLines.length);

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
    const lines = splitLines(text);
    lines.forEach(line => {
        appendPlainLine(container, line, type);
    });
}

async function submitBatchDecisions() {
    if (!currentPendingBatch) return;

    const cards = document.querySelectorAll('#edit-cards .edit-card');
    const decisions = [];

    cards.forEach(card => {
        decisions.push({
            path: card.dataset.path,
            action: card.dataset.decision || 'approve'
        });
    });

    try {
        const res = await fetch(`${API}/resolve_edit`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                toolCallId: currentPendingBatch.toolCallId,
                decisions
            })
        });

        if (res.ok) {
            const result = await res.json();
            const approved = result.applied || [];
            const rejected = result.rejected || [];

            let msg = '';
            if (approved.length) msg += `Approved: ${approved.join(', ')}`;
            if (rejected.length) msg += `${msg ? '. ' : ''}Rejected: ${rejected.join(', ')}`;
            showToast(msg || 'Batch resolved');

            addHistoryItem(currentPendingBatch, approved, rejected);
            currentPendingBatch = null;
            document.getElementById('edit-panel').classList.remove('open');
        } else {
            const data = await res.json();
            showToast(data.detail || 'Failed to resolve edits');
        }
    } catch (e) {
        console.error(e);
        showToast('Error resolving edits');
    }
}

function addHistoryItem(batch, approved, rejected) {
    const list = document.getElementById('edit-history-list');
    const div = document.createElement('div');

    const allApproved = rejected.length === 0;
    const allRejected = approved.length === 0;
    div.className = `history-item ${allRejected ? 'rejected' : 'approved'}`;

    const fileSummaries = batch.files.map(f => {
        const wasApproved = approved.includes(f.path);
        return `<span class="${wasApproved ? 'hist-approved' : 'hist-rejected'}">${f.path}</span>`;
    }).join(', ');

    div.innerHTML = `
        <strong>${batch.summary}</strong><br>
        <small>${fileSummaries}</small>
    `;
    list.prepend(div);
}
