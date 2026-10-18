// Attachment composer: manages files the user attaches to the next message
// (via the 📎 button, file picker, clipboard paste, or a side-panel
// screenshot), renders a removable preview strip, and hands the attachments to
// the send path.
//
// Generalized from image-only to files. Images keep a thumbnail preview (the FE
// already holds their bytes); non-image files (e.g. PDFs) show a named pill —
// no page count, no rendering on the FE. The backend (prep_file) does all
// rendering/extraction. Each attachment is shipped as a {name, dataUrl} object
// so the server learns the original filename.

let pending = [];           // array of { id, dataUrl, name, isImage }
let nextId = 1;

const MAX_ATTACHMENTS = 6;
const MAX_BYTES = 20 * 1024 * 1024; // ~20MB per file (PDFs run larger than images)

// Accepted non-image types. Images are accepted via the `image/` prefix below.
const ACCEPTED_NON_IMAGE_MIMES = new Set(['application/pdf']);

function previewEl() {
    return document.getElementById('image-attachment-preview');
}

function isAcceptedMime(mime) {
    if (!mime) return false;
    return mime.startsWith('image/') || ACCEPTED_NON_IMAGE_MIMES.has(mime);
}

function mimeFromDataUrl(dataUrl) {
    const m = /^data:([a-zA-Z0-9.+/-]+);base64,/.exec(dataUrl || '');
    return m ? m[1].toLowerCase() : '';
}

/** Returns the attachments to send: {name, dataUrl} objects, in insertion order. */
export function getPendingAttachments() {
    return pending.map(p => ({ name: p.name, dataUrl: p.dataUrl }));
}

/** True if the user has at least one attachment staged. */
export function hasPendingAttachments() {
    return pending.length > 0;
}

/** Clear staged attachments and the preview strip (call after a message sends). */
export function clearPendingAttachments() {
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
        chip.className = p.isImage ? 'image-chip' : 'file-chip';

        if (p.isImage) {
            const img = document.createElement('img');
            img.src = p.dataUrl;
            img.alt = p.name || 'attachment';
            chip.appendChild(img);
        } else {
            // Non-image: a named pill (icon + filename), no preview.
            const icon = document.createElement('span');
            icon.className = 'file-chip-icon';
            icon.textContent = '📄';
            chip.appendChild(icon);

            const label = document.createElement('span');
            label.className = 'file-chip-name';
            label.textContent = p.name || 'file';
            label.title = p.name || 'file';
            chip.appendChild(label);
        }

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
    if (!dataUrl || typeof dataUrl !== 'string' || !dataUrl.startsWith('data:')) return false;
    const mime = mimeFromDataUrl(dataUrl);
    if (!isAcceptedMime(mime)) return false;
    if (pending.length >= MAX_ATTACHMENTS) return false;
    const isImage = mime.startsWith('image/');
    pending.push({ id: nextId++, dataUrl, name: name || (isImage ? 'image' : 'file'), isImage });
    renderPreview();
    return true;
}

function readFileAsDataUrl(file) {
    return new Promise((resolve, reject) => {
        if (file.size > MAX_BYTES) {
            reject(new Error('File too large (max 20MB)'));
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
        if (!isAcceptedMime(file.type)) {
            if (onError) onError(`unsupported file type: ${file.name || file.type || 'unknown'}`);
            continue;
        }
        try {
            const dataUrl = await readFileAsDataUrl(file);
            const added = addDataUrl(dataUrl, file.name);
            if (!added && onError) onError('could not attach file (too many attachments?)');
        } catch (e) {
            if (onError) onError(e.message || 'could not read file');
        }
    }
}

/**
 * Wire up the attach button, hidden file input, and clipboard paste on the
 * message textarea. `onError` is an optional toast-style callback.
 */
export function initAttachments(onError) {
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

    // Screenshots from the Chrome side panel arrive via postMessage from the
    // extension (sidepanel.js). The panel captures the visible browser tab,
    // then hands us a PNG data URL — we treat it exactly like a pasted image.
    // The data URL is validated by addDataUrl (must be an accepted mime), and
    // we ignore any message that isn't our expected shape.
    window.addEventListener('message', (e) => {
        const data = e.data;
        if (!data || data.type !== 'localassist:screenshot') return;
        if (typeof data.dataUrl !== 'string') return;
        const added = addDataUrl(data.dataUrl, 'screenshot.png');
        if (!added && onError) {
            onError('could not attach screenshot (too many attachments or bad format)');
        }
    });

    if (textarea) {
        textarea.addEventListener('paste', async (e) => {
            const items = e.clipboardData && e.clipboardData.items;
            if (!items) return;
            const files = [];
            for (const item of items) {
                if (item.kind === 'file' && isAcceptedMime(item.type)) {
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
