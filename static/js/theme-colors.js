// Theme colors for JS consumers (Plotly, Lightweight Charts, inline HTML): read from the CSS tokens at
// draw time, so a theme change only needs a redraw. The FALLBACK values (the dark set) are used only if a
// token reads empty, e.g. before the stylesheet applied. This file and tokens.css are the only places
// allowed to hold color literals.
(function () {
    const FALLBACK = {
        'bg-canvas': '#07090c', 'bg-panel': '#0d1117', 'bg-raised': '#161b22', 'bg-hover': '#1c2230',
        'bg-selected': '#212a3a', 'bg-input': '#0a0e14', 'bg-overlay': '#0b1018',
        'border': '#21262d', 'border-strong': '#30363d',
        'text-strong': '#e6edf3', 'text': '#c9d1d9', 'text-muted': '#8b949e', 'text-faint': '#7d8590',
        'accent': '#f0b429', 'accent-hover': '#ffc94d', 'accent-bg': 'rgba(240, 180, 41, .12)', 'accent-fg': '#07090c',
        'up': '#3fb950', 'up-strong': '#7ee787', 'up-bg': 'rgba(63, 185, 80, .14)', 'up-solid': '#17402a',
        'down': '#f85149', 'down-strong': '#ffa198', 'down-bg': 'rgba(248, 81, 73, .14)', 'down-solid': '#5c1d1f',
        'info': '#58a6ff', 'info-bg': 'rgba(88, 166, 255, .14)', 'alt': '#bc8cff', 'alt-bg': 'rgba(188, 140, 255, .14)',
        'itm-call': 'rgba(63, 185, 80, .10)', 'itm-put': 'rgba(248, 81, 73, .10)',
        'flash-up': 'rgba(63, 185, 80, .38)', 'flash-down': 'rgba(248, 81, 73, .38)',
    };
    const camel = (n) => n.replace(/-([a-z])/g, (_, ch) => ch.toUpperCase());
    let cache = null;

    function read() {
        const cs = getComputedStyle(document.documentElement);
        const out = {};
        for (const name of Object.keys(FALLBACK)) {
            out[camel(name)] = (cs.getPropertyValue('--' + name) || '').trim() || FALLBACK[name];
        }
        return out;
    }

    window.themeColors = function () { return cache || (cache = read()); };
    window.hexToRgb = function (hex) {
        const h = hex.replace('#', '');
        return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
    };
    // Plotly and Lightweight Charts cannot parse color-mix(): give them rgba().
    window.withAlpha = function (hex, a) {
        const [r, g, b] = window.hexToRgb(hex);
        return `rgba(${r},${g},${b},${a})`;
    };
    window.addEventListener('themechange', () => { cache = null; });     // registered before any chart listener
})();
