// Theme resolution: the stored choice (localStorage 'spx-theme' = 'dark' | 'light'), else the OS setting.
// Runs blocking in <head> so data-theme is set before first paint. Storage may be unavailable (private
// window, blocked site data): every access is wrapped, and the page then just follows the OS.
(function () {
    const KEY = 'spx-theme';
    const mq = window.matchMedia ? window.matchMedia('(prefers-color-scheme: light)') : null;

    function stored() {
        try {
            const v = localStorage.getItem(KEY);
            return v === 'dark' || v === 'light' ? v : null;
        } catch (e) { return null; }
    }
    function osTheme() { return mq && mq.matches ? 'light' : 'dark'; }

    function apply(theme) {
        const root = document.documentElement;
        const prev = root.getAttribute('data-theme');
        root.setAttribute('data-theme', theme);
        if (prev !== theme) window.dispatchEvent(new CustomEvent('themechange', { detail: { theme } }));
    }

    window.getTheme = function () {
        return document.documentElement.getAttribute('data-theme') || stored() || osTheme();
    };
    // theme: 'dark' | 'light' | null (follow the OS and forget the stored choice)
    window.setTheme = function (theme) {
        try {
            if (theme === 'dark' || theme === 'light') localStorage.setItem(KEY, theme);
            else localStorage.removeItem(KEY);
        } catch (e) { /* the choice just does not survive a reload */ }
        apply(theme === 'dark' || theme === 'light' ? theme : osTheme());
    };
    window.toggleTheme = function (ev) {
        if (ev && ev.shiftKey) { window.setTheme(null); return; }
        window.setTheme(window.getTheme() === 'light' ? 'dark' : 'light');
    };

    if (mq) {
        const onOsChange = () => { if (!stored()) apply(osTheme()); };
        if (mq.addEventListener) mq.addEventListener('change', onOsChange);
        else if (mq.addListener) mq.addListener(onOsChange);
    }
    apply(stored() || osTheme());
})();
