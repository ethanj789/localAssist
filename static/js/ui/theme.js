import { THEMES, FONTS } from '../constants.js';
import { lsSet, lsGet } from '../utils/storage.js';

export function applyTheme(id) {
    const vars = THEMES[id] ?? THEMES.default;
    const root = document.documentElement;
    Object.entries(vars).forEach(([k, v]) => {
        if (k !== '--sans') root.style.setProperty(k, v);
    });
    lsSet('theme', id);
    // Re-apply the current font so it wins over anything the theme might've set
    applyFont(lsGet('font') ?? 'dm');
}

export function applyFont(id) {
    document.documentElement.style.setProperty('--sans', FONTS[id] ?? FONTS.dm);
    lsSet('font', id);
}

export function initThemes() {
    const themeSelect = document.getElementById('theme-select');
    if (themeSelect) {
        themeSelect.addEventListener('change', e => applyTheme(e.target.value));
        themeSelect.value = lsGet('theme') ?? 'default';
    }

    const fontSelect = document.getElementById('font-select');
    if (fontSelect) {
        fontSelect.addEventListener('change', e => applyFont(e.target.value));
        fontSelect.value = lsGet('font') ?? 'dm';
    }

    // restore on load
    applyTheme(lsGet('theme') ?? 'default');
    applyFont(lsGet('font') ?? 'dm');
}
