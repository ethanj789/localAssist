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
        renderMemories(data.agent_managed || []);
    } catch (e) {
        console.error(e);
        slotsContainer.innerHTML = '<div class="empty-state">Error loading memories</div>';
    }
}

function renderMemories(slots) {
    if (slots.length === 0) {
        slotsContainer.innerHTML = '<div class="empty-state">No agent-managed memories yet.</div>';
        return;
    }

    slotsContainer.innerHTML = '';
    slots.forEach(slot => {
        const div = document.createElement('div');
        div.className = 'memory-slot';
        div.innerHTML = `
            <div class="slot-header">
                <span class="slot-index">Slot #${slot.index}</span>
                <button class="btn-delete-slot" data-index="${slot.index}">Delete</button>
            </div>
            <div class="slot-content">${slot.content}</div>
        `;
        
        div.querySelector('.btn-delete-slot').addEventListener('click', () => deleteSlot(slot.index));
        slotsContainer.appendChild(div);
    });
}

async function deleteSlot(index) {
    if (!confirm(`Delete memory slot #${index}?`)) return;
    
    try {
        const res = await fetch(`${API}/memory/${index}`, { method: 'DELETE' });
        if (res.ok) {
            showToast(`Deleted slot #${index}`);
            await refreshMemories();
        } else {
            showToast('Delete failed');
        }
    } catch (e) {
        console.error(e);
        showToast('Error deleting memory');
    }
}
