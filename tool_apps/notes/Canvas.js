class CanvasManager {
    constructor(canvasElement) {
        this.canvas = canvasElement;
        this.ctx = this.canvas.getContext('2d', { desynchronized: true });
        
        // Logical state
        this.strokes = []; // Array of stroke objects
        this.currentStroke = null;
        
        // Current drawing parameters
        this.color = '#ffffff';
        this.baseWidth = 3;
        this.tool = 'pen'; // 'pen' or 'eraser'
        
        // Set fixed canvas size for now (e.g. standard page size)
        this.width = 1200;
        this.height = 1600;

        // View transform for panning and zooming
        this.transform = { x: 0, y: 0, scale: 1 };
        
        this._initCanvas();
    }
    
    _initCanvas() {
        this.canvas.width = this.width;
        this.canvas.height = this.height;
        this.clear();
    }
    
    clear() {
        this.ctx.fillStyle = '#222222';
        this.ctx.fillRect(0, 0, this.width, this.height);
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
            color: this.tool === 'eraser' ? '#222222' : this.color,
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
        this.strokes.push(stroke);
        this.currentStroke = null;
        
        return stroke;
    }
    
    _applyTransform() {
        this.ctx.translate(this.transform.x, this.transform.y);
        this.ctx.scale(this.transform.scale, this.transform.scale);
    }

    _drawPoint(p, baseWidth, color) {
        this.ctx.save();
        this._applyTransform();
        this.ctx.beginPath();
        const r = (baseWidth * p.pressure) / 2;
        this.ctx.arc(p.x, p.y, Math.max(r, 0.5), 0, Math.PI * 2);
        this.ctx.fillStyle = color;
        this.ctx.fill();
        this.ctx.restore();
    }
    
    _drawLineSegment(p1, p2, baseWidth, color) {
        this.ctx.save();
        this._applyTransform();
        this.ctx.beginPath();
        this.ctx.moveTo(p1.x, p1.y);
        this.ctx.lineTo(p2.x, p2.y);
        
        // Average pressure for segment
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
    
    getStrokesData() {
        return { strokes: this.strokes };
    }
}
