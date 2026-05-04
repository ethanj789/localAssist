import { WEEK_MS } from '../constants.js';

export function lsSet(key, value) {
    localStorage.setItem(key, JSON.stringify({ value, ts: Date.now() }));
}

export function lsGet(key, fallback = null) {
    try {
        const raw = localStorage.getItem(key);
        if (!raw) return fallback;
        const { value, ts } = JSON.parse(raw);
        if (Date.now() - ts > WEEK_MS) {
            localStorage.removeItem(key);
            return fallback;
        }
        return value;
    } catch {
        return fallback;
    }
}
