class PointerEventsInputSource {
    constructor(canvasElement) {
        this.canvas = canvasElement;
        
        this.onStartCb = null;
        this.onPointCb = null;
        this.onEndCb = null;
        
        this.isDrawing = false;
        
        this._bindEvents();
    }
    
    onStrokeStart(cb) { this.onStartCb = cb; }
    onStrokePoint(cb) { this.onPointCb = cb; }
    onStrokeEnd(cb) { this.onEndCb = cb; }
    
    _createInkPoint(e) {
        const rect = this.canvas.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const y = e.clientY - rect.top;
        
        // Pressure normalization. Mouse returns 0.5 usually, Pen returns 0-1.
        let pressure = e.pressure;
        if (e.pointerType === 'mouse' && pressure === 0 && e.buttons > 0) {
            pressure = 0.5;
        }
        if (pressure === undefined || pressure === 0) {
            pressure = 0.5; // fallback
        }
        
        return {
            x,
            y,
            pressure,
            tiltX: e.tiltX || 0,
            tiltY: e.tiltY || 0,
            pointerType: e.pointerType || 'mouse',
            timestamp: Date.now()
        };
    }
    
    _bindEvents() {
        this.canvas.addEventListener('contextmenu', e => e.preventDefault());
        
        this.canvas.addEventListener('pointerdown', (e) => {
            if (e.button !== 0 && e.button !== 1 && e.button !== 2 && e.pointerType === 'mouse') return;
            this.activeButton = e.button;
            this.isDrawing = true;
            this.canvas.setPointerCapture(e.pointerId);
            if (this.onStartCb) {
                const point = this._createInkPoint(e);
                point.button = this.activeButton;
                this.onStartCb(point);
            }
        });
        
        this.canvas.addEventListener('pointermove', (e) => {
            if (!this.isDrawing) return;
            // Handle coalesced events for higher fidelity if available
            if (e.getCoalescedEvents) {
                const events = e.getCoalescedEvents();
                for (let evt of events) {
                    const point = this._createInkPoint(evt);
                    point.button = this.activeButton;
                    if (this.onPointCb) this.onPointCb(point);
                }
            } else {
                if (this.onPointCb) {
                    const point = this._createInkPoint(e);
                    point.button = this.activeButton;
                    this.onPointCb(point);
                }
            }
        });
        
        const endHandler = (e) => {
            if (!this.isDrawing) return;
            this.isDrawing = false;
            this.canvas.releasePointerCapture(e.pointerId);
            if (this.onEndCb) {
                const point = this._createInkPoint(e);
                point.button = this.activeButton;
                this.onEndCb(point);
            }
        };
        
        this.canvas.addEventListener('pointerup', endHandler);
        this.canvas.addEventListener('pointercancel', endHandler);
    }
}
