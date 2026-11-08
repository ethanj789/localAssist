/**
 * dashboard.js — the "home panel" shown in #messages whenever a chat is empty.
 *
 * It is pure UI chrome: nothing here enters the conversation history, so the
 * append-only / KV-cache discipline is untouched. startNewChat() renders it;
 * sending the first message (or opening a conversation) clears #messages, which
 * removes it, and the live clock timer is torn down on the next render.
 *
 * Extensible via a small CARD REGISTRY — each card is { id, title, render }.
 * To add a future card (e.g. gcalendar), push one entry into CARDS.
 */
import { API } from '../constants.js';

// Teardown handles for anything that needs cleanup between renders (timers,
// abort controllers). Reset on each renderDashboard call.
let _clockTimer = null;

const DASH_CITY_KEY = 'dashboard_city';

function getCity() {
    try { return localStorage.getItem(DASH_CITY_KEY) || ''; }
    catch { return ''; }
}

export function setDashboardCity(city) {
    try { localStorage.setItem(DASH_CITY_KEY, (city || '').trim()); }
    catch { /* ignore */ }
}

// ── Card: clock / date ──────────────────────────────────────────────────────
// Fully client-side, ticks every second. No network.

function renderClockCard() {
    const card = document.createElement('div');
    card.className = 'dash-card dash-clock';
    card.innerHTML = `
        <div class="dash-clock-time" id="dash-clock-time">—</div>
        <div class="dash-clock-date" id="dash-clock-date"></div>
    `;

    const timeEl = card.querySelector('#dash-clock-time');
    const dateEl = card.querySelector('#dash-clock-date');

    const paintTime = () => {
        const now = new Date();
        timeEl.textContent = now.toLocaleTimeString([], {
            hour: '2-digit', minute: '2-digit', second: '2-digit',
        });
        dateEl.textContent = now.toLocaleDateString([], {
            weekday: 'long', year: 'numeric', month: 'long', day: 'numeric',
        });
    };

    // Paint immediately so the clock shows a real value on first render. The
    // card isn't appended to the DOM yet at this point, so we must NOT gate this
    // on document.body.contains(card) — that check only belongs in the interval
    // (to tear the timer down once the card is later removed on chat change).
    paintTime();

    if (_clockTimer) clearInterval(_clockTimer);
    _clockTimer = setInterval(() => {
        if (!document.body.contains(card)) {
            clearInterval(_clockTimer);
            _clockTimer = null;
            return;
        }
        paintTime();
    }, 1000);

    return card;
}

// ── Card: weather ────────────────────────────────────────────────────────────
// Populated from the shared /api/dashboard payload (same fetch as news).

function renderWeatherCard(payload) {
    const card = document.createElement('div');
    card.className = 'dash-card dash-weather';

    const w = payload && payload.weather;
    if (!w) {
        card.innerHTML = `
            <div class="dash-card-title">weather</div>
            <div class="dash-weather-empty">weather unavailable</div>
        `;
        return card;
    }

    const rain = (w.rain_chance != null) ? `rain ${w.rain_chance}%` : '';
    const hilo = (w.hi_c != null && w.lo_c != null)
        ? `↑ ${w.hi_c}°  ↓ ${w.lo_c}°` : '';

    card.innerHTML = `
        <div class="dash-card-title">weather · ${esc(w.location || '')}</div>
        <div class="dash-weather-main">
            <span class="dash-weather-temp">${w.temp_c != null ? w.temp_c + '°C' : '—'}</span>
            <span class="dash-weather-desc">${esc(w.desc || '')}</span>
        </div>
        <div class="dash-weather-meta">
            ${w.feels_c != null ? `feels ${w.feels_c}°` : ''}
            ${hilo ? `· ${hilo}` : ''}
            ${rain ? `· ${rain}` : ''}
        </div>
    `;
    return card;
}

// ── Card: news ────────────────────────────────────────────────────────────────

function renderNewsCard(payload) {
    const card = document.createElement('div');
    card.className = 'dash-card dash-news';

    const items = (payload && payload.news) || [];
    if (!items.length) {
        card.innerHTML = `
            <div class="dash-card-title">news</div>
            <div class="dash-news-empty">no headlines right now</div>
        `;
        return card;
    }

    const rows = items.map(n => `
        <a class="dash-news-item" href="${esc(n.url)}" target="_blank" rel="noopener noreferrer" title="${esc(n.title)}">
            <span class="dash-news-title">${esc(n.title)}</span>
            ${n.source ? `<span class="dash-news-source">${esc(n.source)}</span>` : ''}
        </a>
    `).join('');

    card.innerHTML = `<div class="dash-card-title">news</div>
        <div class="dash-news-list">${rows}</div>`;
    return card;
}

// ── Card registry ──────────────────────────────────────────────────────────────
// Order here = render order. `render(ctx)` receives { payload } where payload is
// the /api/dashboard response (or null before/if it fails). Network cards should
// degrade gracefully when payload is null.
const CARDS = [
    { id: 'clock',   render: () => renderClockCard() },
    { id: 'weather', render: (ctx) => renderWeatherCard(ctx.payload) },
    { id: 'news',    render: (ctx) => renderNewsCard(ctx.payload) },
];

// ── Orchestration ──────────────────────────────────────────────────────────────

/**
 * Render the home panel into `container` (the #messages element). Cards that
 * need no network render immediately; the network payload fills the rest in
 * when it arrives. Safe to call repeatedly — each call rebuilds from scratch.
 */
export function renderDashboard(container) {
    if (!container) return;
    if (_clockTimer) { clearInterval(_clockTimer); _clockTimer = null; }

    const root = document.createElement('div');
    root.className = 'dashboard';
    root.id = 'dashboard';

    const grid = document.createElement('div');
    grid.className = 'dash-grid';
    root.appendChild(grid);

    // First paint with no payload — clock is live immediately, network cards
    // show their placeholder until the fetch resolves.
    const paint = (payload) => {
        // Bail if the dashboard was removed (chat changed) while fetching.
        if (!document.body.contains(root)) return;
        grid.innerHTML = '';
        CARDS.forEach(card => {
            try {
                const el = card.render({ payload });
                if (el) grid.appendChild(el);
            } catch (e) {
                console.warn(`dashboard card "${card.id}" failed`, e);
            }
        });
    };

    paint(null);
    container.appendChild(root);

    // Fetch the shared weather+news payload, then repaint network cards. The
    // clock timer keeps running across the repaint (renderClockCard resets it).
    const city = getCity();
    const qs = city ? `?city=${encodeURIComponent(city)}` : '';
    fetch(`${API}/api/dashboard${qs}`)
        .then(r => r.ok ? r.json() : null)
        .then(payload => paint(payload))
        .catch(err => console.warn('dashboard fetch failed', err));
}

/** Tear down any running timers (called when leaving the empty state). */
export function clearDashboard() {
    if (_clockTimer) { clearInterval(_clockTimer); _clockTimer = null; }
}

function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
}
