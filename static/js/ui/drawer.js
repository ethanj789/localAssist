/**
 * Gives each `.drawer-panel` a backdrop scrim with click-outside-to-close,
 * mirroring the panel's `.open` class onto the backdrop's `.active` class.
 */

const MOBILE_BP = 768;

export function initDrawers() {
    const panels = document.querySelectorAll('.drawer-panel');
    panels.forEach(setupDrawer);

    // Strip the resizer's inline width at mobile width so the CSS bottom-sheet
    // rule (width:100%) can take over.
    const clearInlineWidthOnMobile = () => {
        if (window.innerWidth <= MOBILE_BP) {
            panels.forEach(p => { p.style.width = ''; });
        }
    };
    window.addEventListener('resize', clearInlineWidthOnMobile);
    clearInlineWidthOnMobile();
}

function setupDrawer(panel) {
    // Insert before the panel so it stacks underneath (backdrop z 2000, panel 2001).
    const backdrop = document.createElement('div');
    backdrop.className = 'drawer-backdrop';
    panel.parentNode.insertBefore(backdrop, panel);

    backdrop.addEventListener('click', () => {
        panel.classList.remove('open');
    });

    const sync = () => backdrop.classList.toggle('active', panel.classList.contains('open'));
    const observer = new MutationObserver(sync);
    observer.observe(panel, { attributes: true, attributeFilter: ['class'] });
    sync();
}
