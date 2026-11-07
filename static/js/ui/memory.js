import { API } from '../constants.js';
import { showToast } from '../utils/dom.js';

const modal = document.getElementById('memory-modal');
const slotsContainer = document.getElementById('memory-slots');
const memoryBtn = document.getElementById('memory-btn');
const closeBtn = document.getElementById('close-memory-modal');

export function initMemoryUI() {
    if (memoryBtn) {
        memoryBtn.addEventListener('click', openMemoryModal);
    }
    if (closeBtn) {
        closeBtn.addEventListener('click', closeMemoryModal);
    }
    if (modal) {
        modal.addEventListener('click', (e) => {
            if (e.target === modal) closeMemoryModal();
        });
    }
}

async function openMemoryModal() {
    if (!modal) return;
    modal.classList.add('active');
    document.body.style.overflow = 'hidden'; // Prevent background scrolling
    await refreshMemories();
}

function closeMemoryModal() {
    if (!modal) return;
    modal.classList.remove('active');
    document.body.style.overflow = '';
}

async function refreshMemories() {
    if (!slotsContainer) return;
    slotsContainer.innerHTML = '<div class="empty-state">Loading memories...</div>';
    try {
        const res = await fetch(`${API}/memory`);
        if (!res.ok) throw new Error('Failed to fetch memories');
        const data = await res.json();
        renderMemories(data.agent_managed || {});
    } catch (e) {
        console.error(e);
        slotsContainer.innerHTML = '<div class="empty-state">Error loading memories</div>';
    }
}

function renderMemories(prefs) {
    const keys = Object.keys(prefs || {});
    if (keys.length === 0) {
        slotsContainer.innerHTML = '<div class="empty-state">No agent-managed preferences yet.</div>';
        return;
    }

    slotsContainer.innerHTML = '';
    keys.forEach(key => {
        const slot = prefs[key] || {};
        const value = typeof slot === 'object' ? (slot.value ?? '') : slot;
        const div = document.createElement('div');
        div.className = 'memory-slot';
        div.innerHTML = `
            <div class="slot-header">
                <span class="slot-index">${escapeHtml(key)}</span>
                <button class="btn-delete-slot">Delete</button>
            </div>
            <div class="slot-content">${escapeHtml(value)}</div>
        `;

        div.querySelector('.btn-delete-slot').addEventListener('click', () => deleteSlot(key));
        slotsContainer.appendChild(div);
    });
}

function escapeHtml(s) {
    return String(s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

async function deleteSlot(key) {
    try {
        const res = await fetch(`${API}/memory/${encodeURIComponent(key)}`, { method: 'DELETE' });
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
