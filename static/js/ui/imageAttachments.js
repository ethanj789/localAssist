// Image attachment composer: manages images the user attaches to the next
// message (via the 🖼 button, file picker, or clipboard paste), renders a
// removable preview strip, and hands the data URLs to the send path.

let pending = [];           // array of { id, dataUrl, name }
let nextId = 1;

const MAX_ATTACHMENTS = 6;
const MAX_BYTES = 12 * 1024 * 1024; // ~12MB per image

function previewEl() {
    return document.getElementById('image-attachment-preview');
}

/** Returns the data URLs for the currently pending attachments. */
export function getPendingImages() {
    return pending.map(p => p.dataUrl);
}

/** True if the user has at least one image staged. */
export function hasPendingImages() {
    return pending.length > 0;
}

/** Clear staged attachments and the preview strip (call after a message sends). */
export function clearPendingImages() {
    pending = [];
    renderPreview();
}

function removeAttachment(id) {
    pending = pending.filter(p => p.id !== id);
    renderPreview();
}

function renderPreview() {
    const el = previewEl();
    if (!el) return;
    el.innerHTML = '';
    if (pending.length === 0) {
        el.classList.remove('has-items');
        return;
    }
    el.classList.add('has-items');
    pending.forEach(p => {
        const chip = document.createElement('div');
        chip.className = 'image-chip';

        const img = document.createElement('img');
        img.src = p.dataUrl;
        img.alt = p.name || 'attachment';
        chip.appendChild(img);

        const rm = document.createElement('button');
        rm.className = 'image-chip-remove';
        rm.type = 'button';
        rm.title = 'Remove';
        rm.textContent = '×';
        rm.addEventListener('click', () => removeAttachment(p.id));
        chip.appendChild(rm);

        el.appendChild(chip);
    });
}

function addDataUrl(dataUrl, name) {
    if (!dataUrl || !dataUrl.startsWith('data:image/')) return false;
    if (pending.length >= MAX_ATTACHMENTS) return false;
    pending.push({ id: nextId++, dataUrl, name: name || 'image' });
    renderPreview();
    return true;
}

function readFileAsDataUrl(file) {
    return new Promise((resolve, reject) => {
        if (file.size > MAX_BYTES) {
            reject(new Error('Image too large (max 12MB)'));
            return;
        }
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = () => reject(reader.error || new Error('read failed'));
        reader.readAsDataURL(file);
    });
}

/** Add a list of File objects (from a picker or paste) as attachments. */
export async function addFiles(fileList, onError) {
    for (const file of fileList) {
        if (!file.type || !file.type.startsWith('image/')) continue;
        try {
            const dataUrl = await readFileAsDataUrl(file);
            addDataUrl(dataUrl, file.name);
        } catch (e) {
            if (onError) onError(e.message || 'could not read image');
        }
    }
}

/**
 * Wire up the attach button, hidden file input, and clipboard paste on the
 * message textarea. `onError` is an optional toast-style callback.
 */
export function initImageAttachments(onError) {
    const btn = document.getElementById('attach-image-btn');
    const fileInput = document.getElementById('image-file-input');
    const textarea = document.getElementById('user-input');

    if (btn && fileInput) {
        btn.addEventListener('click', () => fileInput.click());
        fileInput.addEventListener('change', async () => {
            if (fileInput.files && fileInput.files.length) {
                await addFiles(fileInput.files, onError);
            }
            fileInput.value = ''; // allow re-selecting the same file
        });
    }

    if (textarea) {
        textarea.addEventListener('paste', async (e) => {
            const items = e.clipboardData && e.clipboardData.items;
            if (!items) return;
            const files = [];
            for (const item of items) {
                if (item.kind === 'file' && item.type.startsWith('image/')) {
                    const f = item.getAsFile();
                    if (f) files.push(f);
                }
            }
            if (files.length) {
                e.preventDefault(); // don't paste the binary blob as text
                await addFiles(files, onError);
            }
        });
    }

    renderPreview();
}
