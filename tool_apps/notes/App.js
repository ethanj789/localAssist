class NotesApp {
    constructor() {
        this.canvasManager = new CanvasManager(document.getElementById('drawing-canvas'));
        this.inputSource = new PointerEventsInputSource(document.getElementById('drawing-canvas'));
        
        this.undoStack = [];
        this.redoStack = [];
        
        this.currentType = 'notes'; // 'notes' or 'art'
        this.currentPageId = null;
        this.currentPageMeta = null;
        
        this.saveTimeout = null;
        this.isSaving = false;

        // Eraser cursor overlay
        this.eraserCursor = this._createEraserCursor();
        
        this._bindUI();
        this._bindInput();
        this._bindEraserCursor();
        
        // Initial load — try to resume last session
        this._resumeLastSession();
    }

    // ─── Eraser Cursor ────────────────────────────────────────────────────────

    _createEraserCursor() {
        const el = document.createElement('div');
        el.id = 'eraser-cursor';
        el.style.cssText = `
            position: fixed;
            pointer-events: none;
            border: 2px solid rgba(255,255,255,0.7);
            border-radius: 50%;
            z-index: 9999;
            display: none;
            transform: translate(-50%, -50%);
            box-shadow: 0 0 0 1px rgba(0,0,0,0.4);
        `;
        document.body.appendChild(el);
        return el;
    }

    _bindEraserCursor() {
        const canvas = document.getElementById('drawing-canvas');

        const updateSize = () => {
            // Eraser logical width * 5 (matches Canvas.js), then scaled to screen
            const logicalDiameter = this.canvasManager.baseWidth * 5 * this.canvasManager.transform.scale;
            this.eraserCursor.style.width  = logicalDiameter + 'px';
            this.eraserCursor.style.height = logicalDiameter + 'px';
        };

        canvas.addEventListener('pointermove', (e) => {
            if (this.canvasManager.tool === 'eraser') {
                this.eraserCursor.style.display = 'block';
                this.eraserCursor.style.left = e.clientX + 'px';
                this.eraserCursor.style.top  = e.clientY + 'px';
                updateSize();
            }
        });

        canvas.addEventListener('pointerleave', () => {
            this.eraserCursor.style.display = 'none';
        });

        // Also hide/show when tool changes
        document.querySelectorAll('.tool-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                if (btn.dataset.tool !== 'eraser') {
                    this.eraserCursor.style.display = 'none';
                }
            });
        });
    }

    // ─── UI Binding ───────────────────────────────────────────────────────────
    
    _bindUI() {
        // Mode toggle
        document.querySelectorAll('input[name="mode"]').forEach(radio => {
            radio.addEventListener('change', (e) => {
                this.currentType = e.target.value;
                this.loadPagesList();
            });
        });
        
        // New Page — no prompt, instant creation
        document.getElementById('new-page-btn').addEventListener('click', async () => {
            await this.createNewPage('');
        });
        
        // Tool selection
        const canvas = document.getElementById('drawing-canvas');
        document.querySelectorAll('.tool-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
                document.querySelectorAll('.tool-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                this.canvasManager.tool = btn.dataset.tool;
                canvas.classList.toggle('eraser-active', btn.dataset.tool === 'eraser');
            });
        });
        
        // Color & Width
        const colorPicker = document.getElementById('color-picker');
        colorPicker.addEventListener('input', (e) => {
            this.canvasManager.color = e.target.value;
            document.querySelector('.tool-btn[data-tool="pen"]').click();
        });
        
        const widthPicker = document.getElementById('width-picker');
        widthPicker.addEventListener('input', (e) => {
            this.canvasManager.baseWidth = parseInt(e.target.value, 10);
        });
        
        // Undo / Redo
        document.getElementById('undo-btn').addEventListener('click', () => this.undo());
        document.getElementById('redo-btn').addEventListener('click', () => this.redo());
        
        // Zooming
        const canvasEl = document.getElementById('drawing-canvas');
        canvasEl.addEventListener('wheel', (e) => {
            if (!this.currentPageId) return;
            e.preventDefault();

            const zoomSpeed = 0.001;
            const delta = -e.deltaY * zoomSpeed;
            const newScale = Math.max(0.1, Math.min(this.canvasManager.transform.scale * (1 + delta), 10));
            
            const rect = canvasEl.getBoundingClientRect();
            const mouseX = e.clientX - rect.left;
            const mouseY = e.clientY - rect.top;

            const scaleRatio = newScale / this.canvasManager.transform.scale;
            
            this.canvasManager.transform.x = mouseX - (mouseX - this.canvasManager.transform.x) * scaleRatio;
            this.canvasManager.transform.y = mouseY - (mouseY - this.canvasManager.transform.y) * scaleRatio;
            this.canvasManager.transform.scale = newScale;

            this.canvasManager.redraw(this.canvasManager.strokes);
            this._scheduleSave();
        }, { passive: false });

        // Dismiss context menu on outside click
        document.addEventListener('click', () => this._dismissContextMenu());
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') this._dismissContextMenu();
        });
    }
    
    _bindInput() {
        this.inputSource.onStrokeStart((point) => {
            if (!this.currentPageId) return;
            
            if (point.button === 1) { // Middle click
                this.isPanning = true;
                this.lastPanPoint = { x: point.x, y: point.y };
                return;
            }
            this.isPanning = false;

            if (point.button === 2) { // Right click
                this.previousTool = this.canvasManager.tool;
                this.canvasManager.tool = 'eraser';
                document.getElementById('drawing-canvas').classList.add('eraser-active');
            }

            this.canvasManager.startStroke(point);
            this.redoStack = [];
        });
        
        this.inputSource.onStrokePoint((point) => {
            if (!this.currentPageId) return;

            if (this.isPanning) {
                const dx = point.x - this.lastPanPoint.x;
                const dy = point.y - this.lastPanPoint.y;
                this.canvasManager.transform.x += dx;
                this.canvasManager.transform.y += dy;
                this.lastPanPoint = { x: point.x, y: point.y };
                this.canvasManager.redraw(this.canvasManager.strokes);
                return;
            }

            this.canvasManager.addPoint(point);
        });
        
        this.inputSource.onStrokeEnd((point) => {
            if (!this.currentPageId) return;

            if (this.isPanning) {
                this.isPanning = false;
                this._scheduleSave();
                return;
            }

            const stroke = this.canvasManager.endStroke(point);
            
            if (point.button === 2) {
                this.canvasManager.tool = this.previousTool;
                const isEraser = this.previousTool === 'eraser';
                document.getElementById('drawing-canvas').classList.toggle('eraser-active', isEraser);
            }

            if (stroke) {
                this.undoStack.push({ type: 'add_stroke', stroke });
                this._scheduleSave();
            }
        });
    }
    
    // ─── Undo / Redo ─────────────────────────────────────────────────────────

    undo() {
        if (this.undoStack.length === 0) return;
        const action = this.undoStack.pop();
        if (action.type === 'add_stroke') {
            this.canvasManager.strokes.pop();
            this.redoStack.push(action);
            this.canvasManager.redraw(this.canvasManager.strokes);
            this._scheduleSave();
        }
    }
    
    redo() {
        if (this.redoStack.length === 0) return;
        const action = this.redoStack.pop();
        if (action.type === 'add_stroke') {
            this.canvasManager.strokes.push(action.stroke);
            this.undoStack.push(action);
            this.canvasManager.redraw(this.canvasManager.strokes);
            this._scheduleSave();
        }
    }

    // ─── Context Menu (rename / delete) ──────────────────────────────────────

    _dismissContextMenu() {
        const existing = document.getElementById('page-context-menu');
        if (existing) existing.remove();
    }

    _showContextMenu(x, y, page) {
        this._dismissContextMenu();

        const menu = document.createElement('div');
        menu.id = 'page-context-menu';
        menu.style.cssText = `
            position: fixed;
            left: ${x}px;
            top: ${y}px;
            background: #2d2d30;
            border: 1px solid #3e3e42;
            border-radius: 4px;
            padding: 4px 0;
            z-index: 10000;
            min-width: 130px;
            box-shadow: 0 4px 16px rgba(0,0,0,0.5);
            font-size: 13px;
        `;

        const makeItem = (label, color, onClick) => {
            const item = document.createElement('div');
            item.textContent = label;
            item.style.cssText = `
                padding: 7px 14px;
                cursor: pointer;
                color: ${color || '#ccc'};
                transition: background 0.1s;
            `;
            item.addEventListener('mouseenter', () => item.style.background = '#3e3e42');
            item.addEventListener('mouseleave', () => item.style.background = 'transparent');
            item.addEventListener('click', (e) => {
                e.stopPropagation();
                this._dismissContextMenu();
                onClick();
            });
            return item;
        };

        menu.appendChild(makeItem('Rename', null, () => this._renamePage(page)));
        menu.appendChild(makeItem('Delete', '#f44747', () => this._deletePage(page)));

        document.body.appendChild(menu);

        // Ensure menu stays on screen
        const rect = menu.getBoundingClientRect();
        if (rect.right > window.innerWidth) menu.style.left = (x - rect.width) + 'px';
        if (rect.bottom > window.innerHeight) menu.style.top = (y - rect.height) + 'px';
    }

    async _renamePage(page) {
        const newTitle = prompt('Rename page:', page.title || page.id);
        if (newTitle === null) return; // cancelled
        const title = newTitle.trim() || 'Untitled';
        try {
            const res = await fetch(`/api/notes/pages/${page.type}/${page.id}/rename`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ title })
            });
            if (!res.ok) throw new Error('Rename failed');
            if (this.currentPageId === page.id) {
                this.currentPageMeta.title = title;
            }
            await this.loadPagesList();
        } catch (e) {
            console.error('Failed to rename page', e);
        }
    }

    async _deletePage(page) {
        if (!confirm(`Delete "${page.title || page.id}"? This cannot be undone.`)) return;
        try {
            const res = await fetch(`/api/notes/pages/${page.type}/${page.id}`, { method: 'DELETE' });
            if (!res.ok) throw new Error('Delete failed');
            if (this.currentPageId === page.id) {
                this.currentPageId = null;
                this.currentPageMeta = null;
                this.canvasManager.strokes = [];
                this.canvasManager.clear();
                localStorage.removeItem('notes_last_page');
            }
            await this.loadPagesList();
        } catch (e) {
            console.error('Failed to delete page', e);
        }
    }
    
    // ─── API Calls ────────────────────────────────────────────────────────────
    
    async loadPagesList() {
        try {
            const res = await fetch(`/api/notes/pages?type=${this.currentType}`);
            const data = await res.json();
            this.renderPageList(data.pages);
        } catch (e) {
            console.error("Failed to load pages", e);
        }
    }
    
    renderPageList(pages) {
        const listEl = document.getElementById('page-list');
        listEl.innerHTML = '';
        
        pages.forEach(page => {
            const el = document.createElement('div');
            el.className = `page-item ${this.currentPageId === page.id ? 'active' : ''}`;
            el.innerHTML = `
                <img src="/api/notes/pages/${page.type}/${page.id}/page.png?ts=${new Date().getTime()}" onerror="this.style.display='none'" alt="thumb">
                <div class="page-title">${page.title || page.id}</div>
            `;
            el.addEventListener('click', () => this.loadPage(page.type, page.id));
            el.addEventListener('contextmenu', (e) => {
                e.preventDefault();
                e.stopPropagation();
                this._showContextMenu(e.clientX, e.clientY, page);
            });
            listEl.appendChild(el);
        });
    }
    
    async createNewPage(title) {
        try {
            const res = await fetch(`/api/notes/pages`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ type: this.currentType, title: title || 'Untitled' })
            });
            const data = await res.json();
            await this.loadPagesList();
            // Immediately open & focus the new page
            await this.loadPage(data.meta.type, data.meta.id);
        } catch (e) {
            console.error("Failed to create page", e);
        }
    }
    
    async loadPage(type, id) {
        try {
            const res = await fetch(`/api/notes/pages/${type}/${id}`);
            if (!res.ok) throw new Error("Not found");
            const data = await res.json();
            
            this.currentPageId = id;
            this.currentType = type;
            this.currentPageMeta = data.meta;
            
            if (data.meta.viewTransform) {
                this.canvasManager.transform = { ...data.meta.viewTransform };
            } else {
                this.canvasManager.transform = { x: 0, y: 0, scale: 1 };
            }
            
            this.canvasManager.redraw(data.strokes.strokes || []);
            this.undoStack = [];
            this.redoStack = [];
            
            // Sync radio button
            document.querySelector(`input[name="mode"][value="${type}"]`).checked = true;
            
            // Persist last opened page
            localStorage.setItem('notes_last_page', JSON.stringify({ type, id }));

            this.loadPagesList(); // refresh active state
        } catch (e) {
            console.error("Failed to load page", e);
        }
    }

    async _resumeLastSession() {
        // Load the page list first so sidebar is populated regardless
        await this.loadPagesList();

        const saved = localStorage.getItem('notes_last_page');
        if (!saved) return;

        try {
            const { type, id } = JSON.parse(saved);
            // Switch the mode toggle to match
            const radio = document.querySelector(`input[name="mode"][value="${type}"]`);
            if (radio) {
                radio.checked = true;
                this.currentType = type;
            }
            await this.loadPage(type, id);
        } catch (e) {
            // stale / invalid saved state — clear it
            localStorage.removeItem('notes_last_page');
            console.warn('Could not resume last session:', e);
        }
    }
    
    _scheduleSave() {
        if (!this.currentPageId) return;
        
        document.getElementById('save-status').innerText = 'Saving...';
        
        if (this.saveTimeout) {
            clearTimeout(this.saveTimeout);
        }
        
        this.saveTimeout = setTimeout(() => {
            this.saveCurrentPage();
        }, 1000);
    }
    
    async saveCurrentPage() {
        if (!this.currentPageId || this.isSaving) return;
        this.isSaving = true;
        
        this.currentPageMeta.viewTransform = { ...this.canvasManager.transform };
        
        const payload = {
            meta: this.currentPageMeta,
            strokes: this.canvasManager.getStrokesData(),
            pageDataUrl: this.canvasManager.getDataUrl()
        };
        
        try {
            const res = await fetch(`/api/notes/pages/${this.currentType}/${this.currentPageId}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            const data = await res.json();
            this.currentPageMeta.updatedAt = data.updatedAt;
            document.getElementById('save-status').innerText = 'Saved';
        } catch (e) {
            console.error("Failed to save", e);
            document.getElementById('save-status').innerText = 'Save Failed';
        } finally {
            this.isSaving = false;
        }
    }
}
