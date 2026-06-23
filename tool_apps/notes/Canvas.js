class CanvasManager {
    // Background fill used everywhere — eraser strokes match this
    static BG = '#1a1a1a';

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
        // Use device pixel ratio for sharp rendering on HiDPI screens
        const dpr = window.devicePixelRatio || 1;
        const w = container.clientWidth;
        const h = container.clientHeight;
        this.canvas.width  = w * dpr;
        this.canvas.height = h * dpr;
        this.ctx.scale(dpr, dpr);
        // Store CSS dimensions for coordinate math
        this._cssWidth  = w;
        this._cssHeight = h;
    }

    _bindResize() {
        const ro = new ResizeObserver(() => {
            this._resizeToContainer();
            this.redraw(this.strokes);
        });
        ro.observe(this.canvas.parentElement);
    }
    
    /** Fit all strokes into view with padding. Falls back to identity if no strokes. */
    fitContent() {
        const vw = this._cssWidth  || this.canvas.parentElement.clientWidth;
        const vh = this._cssHeight || this.canvas.parentElement.clientHeight;

        if (this.strokes.length === 0) {
            this.transform = { x: 0, y: 0, scale: 1 };
            return;
        }

        let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
        for (const stroke of this.strokes) {
            // Skip eraser strokes — they paint over content and shouldn't influence bounds
            if (stroke.color === CanvasManager.BG) continue;
            for (const p of stroke.points) {
                if (minX > p.x) minX = p.x;
                if (minY > p.y) minY = p.y;
                if (maxX < p.x) maxX = p.x;
                if (maxY < p.y) maxY = p.y;
            }
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
        const dpr = window.devicePixelRatio || 1;
        const w = this.canvas.width  / dpr;
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
        this._drawPoint({ x: lp.x, y: lp.y, pressure: point.pressure }, this.currentStroke.width, this.currentStroke.color);
    }
    
    addPoint(point) {
        if (!this.currentStroke) return;
        
        const lastPoint = this.currentStroke.points[this.currentStroke.points.length - 1];
        const lp = this.getLogicalPoint(point.x, point.y);
        
        const newPoint = {
            x: lp.x,
            y: lp.y,
            pressure: point.pressure,
            t: point.timestamp
        };
        this.currentStroke.points.push(newPoint);
        
        this._drawLineSegment(lastPoint, newPoint, this.currentStroke.width, this.currentStroke.color);
    }
    
    endStroke(point) {
        if (!this.currentStroke) return null;
        
        this.addPoint(point);
        const stroke = this.currentStroke;
        // Simplify point geometry before storing — plain RDP, tolerance 0.5 logical px
        stroke.points = CanvasManager._rdpSimplify(stroke.points, 0.5);
        this.strokes.push(stroke);
        this.currentStroke = null;
        
        return stroke;
    }

    /**
     * Ramer-Douglas-Peucker polyline simplification.
     * Drops points whose perpendicular distance from the chord p[0]→p[last]
     * is within `tolerance` logical pixels.
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
            const left  = CanvasManager._rdpSimplify(points.slice(0, maxIdx + 1), tolerance);
            const right = CanvasManager._rdpSimplify(points.slice(maxIdx),         tolerance);
            return [...left.slice(0, -1), ...right];
        }
        return [points[0], points[points.length - 1]];
    }
    
    _applyTransform() {
        this.ctx.translate(this.transform.x, this.transform.y);
        this.ctx.scale(this.transform.scale, this.transform.scale);
    }

    _drawPoint(p, baseWidth, color) {
        const dpr = window.devicePixelRatio || 1;
        this.ctx.save();
        this.ctx.setTransform(1, 0, 0, 1, 0, 0);
        this.ctx.scale(dpr, dpr);
        this._applyTransform();
        this.ctx.beginPath();
        const r = (baseWidth * p.pressure) / 2;
        this.ctx.arc(p.x, p.y, Math.max(r, 0.5), 0, Math.PI * 2);
        this.ctx.fillStyle = color;
        this.ctx.fill();
        this.ctx.restore();
    }
    
    _drawLineSegment(p1, p2, baseWidth, color) {
        const dpr = window.devicePixelRatio || 1;
        this.ctx.save();
        this.ctx.setTransform(1, 0, 0, 1, 0, 0);
        this.ctx.scale(dpr, dpr);
        this._applyTransform();
        this.ctx.beginPath();
        this.ctx.moveTo(p1.x, p1.y);
        this.ctx.lineTo(p2.x, p2.y);
        
        const pressure = (p1.pressure + p2.pressure) / 2;
        this.ctx.lineWidth = Math.max(baseWidth * pressure, 1);
        this.ctx.lineCap = 'round';
        this.ctx.lineJoin = 'round';
        this.ctx.strokeStyle = color;
        this.ctx.stroke();
        this.ctx.restore();
    }
    
    redraw(strokes) {
        this.strokes = strokes || [];
        this.clear();
        for (let stroke of this.strokes) {
            if (stroke.points.length === 0) continue;
            
            if (stroke.points.length === 1) {
                this._drawPoint(stroke.points[0], stroke.width, stroke.color);
                continue;
            }
            
            for (let i = 1; i < stroke.points.length; i++) {
                this._drawLineSegment(stroke.points[i-1], stroke.points[i], stroke.width, stroke.color);
            }
        }
    }
    
    getDataUrl() {
        return this.canvas.toDataURL('image/png');
    }
    
    /**
     * Serialize strokes to the compact v2 wire/disk format.
     * Each point becomes [x, y, pressure, dt_ms] where dt is ms since stroke start.
     * Numbers are rounded: x/y to 1 decimal, pressure to 2 decimals.
     */
    getStrokesData() {
        const strokes = this.strokes.map(stroke => {
            const t0 = stroke.points.length > 0 ? (stroke.points[0].t || 0) : 0;
            return {
                id:     stroke.id,
                color:  stroke.color,
                width:  stroke.width,
                t0,
                points: stroke.points.map(p => [
                    Math.round(p.x * 10) / 10,
                    Math.round(p.y * 10) / 10,
                    Math.round(p.pressure * 100) / 100,
                    Math.round((p.t || t0) - t0)
                ])
            };
        });
        return { v: 2, strokes };
    }
}
