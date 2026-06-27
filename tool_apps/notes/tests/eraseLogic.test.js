const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
const path = require('path');

const source = fs.readFileSync(path.join(__dirname, '..', 'Canvas.js'), 'utf8');
const sandbox = { window: { devicePixelRatio: 1 }, document: { createElement: () => ({ getContext: () => ({}) }) } };
vm.createContext(sandbox);
vm.runInContext(source + '\nthis.CanvasManager = CanvasManager;', sandbox);
const CanvasManager = sandbox.CanvasManager;

const ink = {
    id: 'ink',
    color: '#ffffff',
    width: 3,
    points: [
        { x: -5, y: 0, pressure: 1, t: 0 },
        { x: 0, y: 0, pressure: 1, t: 1 },
        { x: 5, y: 0, pressure: 1, t: 2 }
    ]
};

const eraser = {
    id: 'erase',
    color: '#1a1a1a',
    width: 4,
    points: [
        { x: 0, y: 0, pressure: 1, t: 0 }
    ]
};

const fragments = CanvasManager._eraseStrokeWithEraser(ink, eraser);
assert.ok(fragments.length >= 2, 'expected surrounding fragments to survive a local erasure');
assert.ok(fragments.some(fragment => fragment.points.some(p => p.x < 0)), 'expected left fragment to remain');
assert.ok(fragments.some(fragment => fragment.points.some(p => p.x > 0)), 'expected right fragment to remain');
console.log('erase logic regression test passed');
