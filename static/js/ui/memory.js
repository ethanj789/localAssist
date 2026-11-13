import { API } from '../constants.js';
import { showToast } from '../utils/dom.js';

const modal = document.getElementById('memory-modal');
const slotsContainer = document.getElementById('memory-slots');
const memoryBtn = document.getElementById('memory-btn');
const closeBtn = document.getElementById('close-memory-modal');
const addForm = document.getElementById('memory-add-form');
const addKeyInput = document.getElementById('memory-add-key');
const addValueInput = document.getElementById('memory-add-value');
const addBucketInput = document.getElementById('memory-add-bucket');
const bucketSelect = document.getElementById('memory-bucket-select');
const bucketTrigger = document.getElementById('memory-bucket-trigger');
const bucketLabel = document.getElementById('memory-bucket-label');

export function initMemoryUI() {
    if (memoryBtn) {
        memoryBtn.addEventListener('click', openMemoryModal);
    }
    if (closeBtn) {
        closeBtn.addEventListener('click', closeMemoryModal);
    }
    if (addForm) {
        addForm.addEventListener('submit', onAddSubmit);
    }
    initBucketSelect();
}

function initBucketSelect() {
    if (!bucketSelect || !bucketTrigger) return;
    const options = bucketSelect.querySelectorAll('.chip-option');

    bucketTrigger.addEventListener('click', (e) => {
        e.stopPropagation();
        bucketSelect.classList.toggle('open');
    });

    options.forEach(opt => {
        opt.addEventListener('click', () => {
            const val = opt.getAttribute('data-value');
            const icon = opt.querySelector('.chip-opt-icon')?.textContent || '📌';
            const label = opt.querySelector('.chip-opt-label')?.textContent.trim()
                || opt.textContent.trim();

            if (addBucketInput) addBucketInput.value = val;
            bucketTrigger.innerHTML = `<span class="chip-icon">${icon}</span> <span id="memory-bucket-label">${label}</span>`;

            options.forEach(o => o.classList.remove('active'));
            opt.classList.add('active');
            bucketSelect.classList.remove('open');
        });
    });

    // Close when clicking outside the dropdown.
    document.addEventListener('click', (e) => {
        if (!bucketSelect.contains(e.target)) bucketSelect.classList.remove('open');
    });
}

async function openMemoryModal() {
    if (!modal) return;
    modal.classList.add('open');
    document.body.style.overflow = 'hidden'; // Prevent background scrolling
    await refreshMemories();
}

function closeMemoryModal() {
    if (!modal) return;
    modal.classList.remove('open');
    document.body.style.overflow = '';
}

async function refreshMemories() {
    if (!slotsContainer) return;
    slotsContainer.innerHTML = '<div class="empty-state">Loading memories...</div>';
    try {
        const res = await fetch(`${API}/memory`);
        if (!res.ok) throw new Error('Failed to fetch memories');
        const data = await res.json();
        renderMemories(data.manual || {}, data.agent_managed || {});
    } catch (e) {
        console.error(e);
        slotsContainer.innerHTML = '<div class="empty-state">Error loading memories</div>';
    }
}

function renderMemories(manual, agentManaged) {
    const manualKeys = Object.keys(manual || {});
    const agentKeys = Object.keys(agentManaged || {});

    if (manualKeys.length === 0 && agentKeys.length === 0) {
        slotsContainer.innerHTML = '<div class="empty-state">No memories yet.</div>';
        return;
    }

    slotsContainer.innerHTML = '';
    renderSection('Manual', 'manual', manual, manualKeys);
    renderSection('Agent-managed', 'agent_managed', agentManaged, agentKeys);
}

function renderSection(label, bucket, prefs, keys) {
    const header = document.createElement('div');
    header.className = 'memory-section-label';
    header.textContent = label;
    slotsContainer.appendChild(header);

    if (keys.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'empty-state empty-state-inline';
        empty.textContent = bucket === 'manual'
            ? 'No manual entries.'
            : 'No agent-managed preferences yet.';
        slotsContainer.appendChild(empty);
        return;
    }

    keys.forEach(key => {
        const slot = prefs[key] || {};
        const value = typeof slot === 'object' ? (slot.value ?? '') : slot;
        const div = document.createElement('div');
        div.className = `memory-slot bucket-${bucket}`;
        div.innerHTML = `
            <div class="slot-header">
                <span class="slot-index">${escapeHtml(key)}</span>
                <button class="btn-delete-slot">Delete</button>
            </div>
            <div class="slot-content">${escapeHtml(value)}</div>
        `;

        div.querySelector('.btn-delete-slot').addEventListener('click', () => deleteSlot(bucket, key));
        slotsContainer.appendChild(div);
    });
}

function escapeHtml(s) {
    return String(s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

async function onAddSubmit(e) {
    e.preventDefault();
    const key = (addKeyInput?.value || '').trim();
    const value = (addValueInput?.value || '').trim();
    const bucket = addBucketInput?.value || 'manual';
    if (!key || !value) {
        showToast('Both key and value are required');
        return;
    }
    await addSlot(key, value, bucket);
}

async function addSlot(key, value, bucket) {
    try {
        const res = await fetch(`${API}/memory`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ key, value, bucket }),
        });
        if (res.ok) {
            showToast(`Saved "${key}"`);
            if (addKeyInput) addKeyInput.value = '';
            if (addValueInput) addValueInput.value = '';
            await refreshMemories();
        } else {
            let detail = 'Save failed';
            try {
                const err = await res.json();
                if (err?.detail) detail = err.detail;
            } catch { /* keep default */ }
            showToast(detail);
        }
    } catch (e) {
        console.error(e);
        showToast('Error saving memory');
    }
}

async function deleteSlot(bucket, key) {
    try {
        const res = await fetch(`${API}/memory/${encodeURIComponent(bucket)}/${encodeURIComponent(key)}`, { method: 'DELETE' });
        if (res.ok) {
            showToast(`Deleted "${key}"`);
            await refreshMemories();
        } else {
            showToast('Delete failed');
        }
    } catch (e) {
        console.error(e);
        showToast('Error deleting memory');
    }
}
