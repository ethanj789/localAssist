class CanvasManager {
    // Background fill used everywhere — eraser strokes match this
    static BG = '#1a1a1a';

    // Geometry simplification — higher = fewer stored points, still looks smooth
    static RDP_TOLERANCE = 1.0;
    static MIN_POINT_DIST = 0.75; // logical px; skip denser samples while drawing

    constructor(canvasElement) {
        this.canvas = canvasElement;
        this.ctx = this.canvas.getContext('2d', { desynchronized: true });

        // Logical state
        this.strokes = [];
        this.currentStroke = null;

        // Current drawing parameters
        this.color = '#ffffff';
        this.baseWidth = 3;
        this.tool = 'pen'; // 'pen' or 'eraser'

        // View transform for panning and zooming
        this.transform = { x: 0, y: 0, scale: 1 };

        // Offscreen bitmap of completed strokes (logical coords) — pan/zoom just blits this
        this._contentLayer = null;

        this._initCanvas();
        this._bindResize();
    }

    _initCanvas() {
        this._resizeToContainer();
        this.clear();
    }

    _resizeToContainer() {
        const container = this.canvas.parentElement;
        if (!container) return;
        const dpr = window.devicePixelRatio || 1;
        const w = container.clientWidth;
        const h = container.clientHeight;
        this.canvas.width = w * dpr;
        this.canvas.height = h * dpr;
        this.ctx.scale(dpr, dpr);
        this._cssWidth = w;
        this._cssHeight = h;
        this._dpr = dpr;
    }

    _bindResize() {
        const ro = new ResizeObserver(() => {
            this._resizeToContainer();
            this.redraw(this.strokes);
        });
        ro.observe(this.canvas.parentElement);
    }

    static _isEraserStroke(stroke) {
        return stroke.color === CanvasManager.BG;
    }

    /** Fit all strokes into view with padding. Falls back to identity if no strokes. */
    fitContent() {
        const vw = this._cssWidth || this.canvas.parentElement.clientWidth;
        const vh = this._cssHeight || this.canvas.parentElement.clientHeight;

        if (this.strokes.length === 0) {
            this.transform = { x: 0, y: 0, scale: 1 };
            return;
        }

        let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
        for (const stroke of this.strokes) {
            if (CanvasManager._isEraserStroke(stroke)) continue;
            for (const p of stroke.points) {
                if (minX > p.x) minX = p.x;
                if (minY > p.y) minY = p.y;
                if (maxX < p.x) maxX = p.x;
                if (maxY < p.y) maxY = p.y;
            }
        }

        if (!isFinite(minX)) {
            this.transform = { x: 0, y: 0, scale: 1 };
            return;
        }

        const padding = 60;
        const cw = maxX - minX || 1;
        const ch = maxY - minY || 1;
        const scale = Math.min((vw - padding * 2) / cw, (vh - padding * 2) / ch);

        this.transform = {
            scale,
            x: (vw - cw * scale) / 2 - minX * scale,
            y: (vh - ch * scale) / 2 - minY * scale,
        };
    }

    clear() {
        const dpr = this._dpr || window.devicePixelRatio || 1;
        const w = this.canvas.width / dpr;
        const h = this.canvas.height / dpr;

        this.ctx.save();
        this.ctx.setTransform(1, 0, 0, 1, 0, 0);
        this.ctx.scale(dpr, dpr);
        this.ctx.fillStyle = CanvasManager.BG;
        this.ctx.fillRect(0, 0, w, h);
        this.ctx.restore();
    }

    generateId() {
        return Math.random().toString(36).substring(2, 9);
    }

    getLogicalPoint(px, py) {
        return {
            x: (px - this.transform.x) / this.transform.scale,
            y: (py - this.transform.y) / this.transform.scale
        };
    }

    startStroke(point) {
        const lp = this.getLogicalPoint(point.x, point.y);
        this.currentStroke = {
            id: 's_' + this.generateId(),
            color: this.tool === 'eraser' ? CanvasManager.BG : this.color,
            width: this.tool === 'eraser' ? this.baseWidth * 5 : this.baseWidth,
            points: [{
                x: lp.x,
                y: lp.y,
                pressure: point.pressure,
                t: point.timestamp
            }]
        };
        this._withViewTransform(this.ctx, (ctx) => {
            this._drawPointInContext(ctx, this.currentStroke.points[0], this.currentStroke.width, this.currentStroke.color);
        });
    }

    addPoint(point) {
        if (!this.currentStroke) return;

        const lastPoint = this.currentStroke.points[this.currentStroke.points.length - 1];
        const lp = this.getLogicalPoint(point.x, point.y);

        const dx = lp.x - lastPoint.x;
        const dy = lp.y - lastPoint.y;
        if (dx * dx + dy * dy < CanvasManager.MIN_POINT_DIST * CanvasManager.MIN_POINT_DIST) {
            return;
        }

        const newPoint = {
            x: lp.x,
            y: lp.y,
            pressure: point.pressure,
            t: point.timestamp
        };
        this.currentStroke.points.push(newPoint);

        this._withViewTransform(this.ctx, (ctx) => {
            this._drawLineSegmentInContext(ctx, lastPoint, newPoint, this.currentStroke.width, this.currentStroke.color);
        });
    }

    endStroke(point) {
        if (!this.currentStroke) return null;

        this.addPoint(point);
        const stroke = this.currentStroke;
        stroke.points = CanvasManager._rdpSimplify(stroke.points, CanvasManager.RDP_TOLERANCE);
        this.strokes.push(stroke);
        this.currentStroke = null;
        this._appendStrokeToLayer(stroke);
        this.redrawView();

        return stroke;
    }

    /**
     * Ramer-Douglas-Peucker polyline simplification.
     */
    static _rdpSimplify(points, tolerance) {
        if (points.length <= 2) return points;

        const p1 = points[0], p2 = points[points.length - 1];
        const dx = p2.x - p1.x, dy = p2.y - p1.y;
        const lineLenSq = dx * dx + dy * dy;

        let maxDist = 0, maxIdx = 0;
        for (let i = 1; i < points.length - 1; i++) {
            const p = points[i];
            let dist;
            if (lineLenSq === 0) {
                const ex = p.x - p1.x, ey = p.y - p1.y;
                dist = Math.sqrt(ex * ex + ey * ey);
            } else {
                const t = ((p.x - p1.x) * dx + (p.y - p1.y) * dy) / lineLenSq;
                const projX = p1.x + t * dx, projY = p1.y + t * dy;
                const ex = p.x - projX, ey = p.y - projY;
                dist = Math.sqrt(ex * ex + ey * ey);
            }
            if (dist > maxDist) { maxDist = dist; maxIdx = i; }
        }

        if (maxDist > tolerance) {
            const left = CanvasManager._rdpSimplify(points.slice(0, maxIdx + 1), tolerance);
            const right = CanvasManager._rdpSimplify(points.slice(maxIdx), tolerance);
            return [...left.slice(0, -1), ...right];
        }
        return [points[0], points[points.length - 1]];
    }

    /**
     * Apply eraser strokes in order, then drop eraser entries.
     * Converts painter's-algorithm erasing into actual ink removal for storage.
     */
    static compactStrokes(strokes) {
        let inkStrokes = [];

        for (const stroke of strokes) {
            if (CanvasManager._isEraserStroke(stroke)) {
                const next = [];
                for (const ink of inkStrokes) {
                    next.push(...CanvasManager._eraseStrokeWithEraser(ink, stroke));
                }
                inkStrokes = next.filter(s => s.points.length > 0 && !CanvasManager._isTinyRemnant(s));
            } else {
                inkStrokes.push({
                    id: stroke.id,
                    color: stroke.color,
                    width: stroke.width,
                    points: stroke.points.map(p => ({ ...p }))
                });
            }
        }

        return inkStrokes
            .map(s => ({
                ...s,
                points: CanvasManager._rdpSimplify(s.points, CanvasManager.RDP_TOLERANCE)
            }))
            .filter(s => s.points.length > 0 && !CanvasManager._isTinyRemnant(s));
    }

    static _strokeLength(stroke) {
        if (!stroke?.points || stroke.points.length < 2) return 0;
        let length = 0;
        for (let i = 1; i < stroke.points.length; i++) {
            const p1 = stroke.points[i - 1];
            const p2 = stroke.points[i];
            length += Math.hypot(p2.x - p1.x, p2.y - p1.y);
        }
        return length;
    }

    static _isTinyRemnant(stroke) {
        if (!stroke || !stroke.points || stroke.points.length === 0) return true;
        if (stroke.points.length === 1) return true;
        const minLength = Math.max(2.5, (stroke.width || 1) * 0.7);
        return CanvasManager._strokeLength(stroke) <= minLength;
    }

    static _eraseStrokeWithEraser(ink, eraser) {
        if (ink.points.length === 0) return [];

        const eraserPts = eraser.points;
        const baseR = eraser.width / 2;

        const isErased = (x, y) => {
            for (const ep of eraserPts) {
                const r = baseR * (ep.pressure || 0.5);
                const dx = x - ep.x, dy = y - ep.y;
                if (dx * dx + dy * dy <= r * r) return true;
            }
            return false;
        };

        const segmentErased = (p1, p2) => {
            const dist = Math.hypot(p2.x - p1.x, p2.y - p1.y);
            const steps = Math.max(1, Math.ceil(dist / 2));
            for (let i = 0; i <= steps; i++) {
                const t = i / steps;
                const x = p1.x + t * (p2.x - p1.x);
                const y = p1.y + t * (p2.y - p1.y);
                if (isErased(x, y)) return true;
            }
            return false;
        };

        const fragments = [];
        let current = [];

        for (let i = 0; i < ink.points.length; i++) {
            const p = ink.points[i];
            const prev = i > 0 ? ink.points[i - 1] : null;
            const pointHit = isErased(p.x, p.y);
            const segHit = prev && segmentErased(prev, p);

            if (segHit) {
                if (current.length >= 1) {
                    const fragment = CanvasManager._cloneStrokeWithPoints(ink, current);
                    if (!CanvasManager._isTinyRemnant(fragment)) fragments.push(fragment);
                }
                current = pointHit ? [] : [p];
            } else if (pointHit) {
                if (current.length >= 1) {
                    const fragment = CanvasManager._cloneStrokeWithPoints(ink, current);
                    if (!CanvasManager._isTinyRemnant(fragment)) fragments.push(fragment);
                }
                current = [];
            } else {
                current.push(p);
            }

        }

        if (current.length >= 1) {
            const fragment = CanvasManager._cloneStrokeWithPoints(ink, current);
            if (!CanvasManager._isTinyRemnant(fragment)) fragments.push(fragment);
        }

        return fragments;
    }

    static _cloneStrokeWithPoints(stroke, points) {
        return {
            id: stroke.id + (points.length < stroke.points.length ? '_' + CanvasManager._fragmentSuffix() : ''),
            color: stroke.color,
            width: stroke.width,
            points: points.map(p => ({ ...p }))
        };
    }

    static _fragmentSuffix() {
        return Math.random().toString(36).substring(2, 5);
    }

    static countPoints(strokes) {
        let n = 0;
        for (const s of strokes) n += s.points.length;
        return n;
    }

    /** In-place compaction when erasers or high point count bloat storage. */
    compactIfNeeded() {
        const hasEraser = this.strokes.some(CanvasManager._isEraserStroke);
        const beforePoints = CanvasManager.countPoints(this.strokes);
        if (!hasEraser && beforePoints < 2000) return false;

        this.strokes = CanvasManager.compactStrokes(this.strokes);
        this._syncContentLayer();
        return hasEraser || CanvasManager.countPoints(this.strokes) < beforePoints;
    }

    _strokeBounds(stroke) {
        let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
        for (const p of stroke.points) {
            if (minX > p.x) minX = p.x;
            if (minY > p.y) minY = p.y;
            if (maxX < p.x) maxX = p.x;
            if (maxY < p.y) maxY = p.y;
        }
        const pad = stroke.width;
        return { minX: minX - pad, minY: minY - pad, maxX: maxX + pad, maxY: maxY + pad };
    }

    _computeStrokeBounds(strokes, includeEraser = true) {
        let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
        for (const stroke of strokes) {
            if (!includeEraser && CanvasManager._isEraserStroke(stroke)) continue;
            for (const p of stroke.points) {
                if (minX > p.x) minX = p.x;
                if (minY > p.y) minY = p.y;
                if (maxX < p.x) maxX = p.x;
                if (maxY < p.y) maxY = p.y;
            }
        }
        if (!isFinite(minX)) return null;
        return { minX, minY, maxX, maxY };
    }

    _layerDpr() {
        return Math.min(2, window.devicePixelRatio || 1);
    }

    _syncContentLayer() {
        if (this.strokes.length === 0) {
            this._contentLayer = null;
            return;
        }

        const bounds = this._computeStrokeBounds(this.strokes, true);
        if (!bounds) {
            this._contentLayer = null;
            return;
        }

        const pad = 64;
        const minX = bounds.minX - pad;
        const minY = bounds.minY - pad;
        const w = Math.ceil(bounds.maxX - bounds.minX + pad * 2);
        const h = Math.ceil(bounds.maxY - bounds.minY + pad * 2);
        const layerDpr = this._layerDpr();

        const canvas = document.createElement('canvas');
        canvas.width = Math.ceil(w * layerDpr);
        canvas.height = Math.ceil(h * layerDpr);
        const ctx = canvas.getContext('2d');
        ctx.scale(layerDpr, layerDpr);
        ctx.fillStyle = CanvasManager.BG;
        ctx.fillRect(0, 0, w, h);
        ctx.translate(-minX, -minY);

        for (const stroke of this.strokes) {
            this._drawStrokeInContext(ctx, stroke);
        }

        this._contentLayer = { canvas, minX, minY, w, h, layerDpr };
    }

    _appendStrokeToLayer(stroke) {
        if (stroke.points.length === 0) return;

        const sb = this._strokeBounds(stroke);

        if (!this._contentLayer) {
            this._syncContentLayer();
            return;
        }

        const L = this._contentLayer;
        const fits = sb.minX >= L.minX && sb.minY >= L.minY
            && sb.maxX <= L.minX + L.w && sb.maxY <= L.minY + L.h;

        if (!fits) {
            this._syncContentLayer();
            return;
        }

        const ctx = L.canvas.getContext('2d');
        ctx.save();
        ctx.setTransform(L.layerDpr, 0, 0, L.layerDpr, 0, 0);
        ctx.translate(-L.minX, -L.minY);
        this._drawStrokeInContext(ctx, stroke);
        ctx.restore();
    }

    _withViewTransform(ctx, fn) {
        const dpr = this._dpr || window.devicePixelRatio || 1;
        ctx.save();
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.translate(this.transform.x, this.transform.y);
        ctx.scale(this.transform.scale, this.transform.scale);
        fn(ctx);
        ctx.restore();
    }

    _drawPointInContext(ctx, p, baseWidth, color) {
        ctx.beginPath();
        const r = (baseWidth * p.pressure) / 2;
        ctx.arc(p.x, p.y, Math.max(r, 0.5), 0, Math.PI * 2);
        ctx.fillStyle = color;
        ctx.fill();
    }

    _drawLineSegmentInContext(ctx, p1, p2, baseWidth, color) {
        ctx.beginPath();
        ctx.moveTo(p1.x, p1.y);
        ctx.lineTo(p2.x, p2.y);
        const pressure = (p1.pressure + p2.pressure) / 2;
        ctx.lineWidth = Math.max(baseWidth * pressure, 1);
        ctx.lineCap = 'round';
        ctx.lineJoin = 'round';
        ctx.strokeStyle = color;
        ctx.stroke();
    }

    _drawStrokeInContext(ctx, stroke) {
        if (stroke.points.length === 0) return;
        if (stroke.points.length === 1) {
            this._drawPointInContext(ctx, stroke.points[0], stroke.width, stroke.color);
            return;
        }
        for (let i = 1; i < stroke.points.length; i++) {
            this._drawLineSegmentInContext(
                ctx,
                stroke.points[i - 1],
                stroke.points[i],
                stroke.width,
                stroke.color
            );
        }
    }

    /** Full repaint — use when strokes change (load, undo, resize). */
    redraw(strokes) {
        this.strokes = strokes || [];
        this._syncContentLayer();
        this.redrawView();
    }

    /** View-only repaint — blits content layer + in-progress stroke (fast pan/zoom). */
    redrawView() {
        this.clear();
        this._withViewTransform(this.ctx, (ctx) => {
            if (this._contentLayer) {
                const L = this._contentLayer;
                ctx.drawImage(L.canvas, L.minX, L.minY, L.w, L.h);
            }
            if (this.currentStroke && this.currentStroke.points.length > 0) {
                this._drawStrokeInContext(ctx, this.currentStroke);
            }
        });
    }

    getDataUrl() {
        return this.canvas.toDataURL('image/png');
    }

    /**
     * Serialize strokes to compact v3 wire/disk format.
     * Each point is [x, y, pressure] — timestamps omitted (not used for rendering).
     */
    getStrokesData() {
        const strokes = this.strokes.map(stroke => ({
            id: stroke.id,
            color: stroke.color,
            width: stroke.width,
            points: stroke.points.map(p => [
                Math.round(p.x * 10) / 10,
                Math.round(p.y * 10) / 10,
                Math.round(p.pressure * 100) / 100
            ])
        }));
        return { v: 3, strokes };
    }

    /** Decode v2/v3 stroke payloads into runtime stroke objects. */
    static decodeStrokesData(data) {
        const raw = data.strokes || [];
        if (data.v === 3) {
            return raw.map(stroke => ({
                id: stroke.id,
                color: stroke.color,
                width: stroke.width,
                points: stroke.points.map(([x, y, pressure]) => ({
                    x, y, pressure: pressure ?? 0.5, t: 0
                }))
            }));
        }
        if (data.v === 2) {
            return raw.map(stroke => ({
                id: stroke.id,
                color: stroke.color,
                width: stroke.width,
                points: stroke.points.map(([x, y, pressure, dt]) => ({
                    x, y, pressure, t: (stroke.t0 || 0) + (dt || 0)
                }))
            }));
        }
        return raw;
    }
}
