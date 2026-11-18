    // Init
    // ======================================================================
    // The tab containers fill the viewport below the header, the levels strip and the tab bar. Their
    // heights are not constants (the strip wraps, fonts differ), so measure them.
    function syncShellHeight() {
        let h = 0;
        for (const sel of ['.header', '#levelsStrip', '.tab-bar']) {
            const el = document.querySelector(sel);
            if (el) h += el.getBoundingClientRect().height;
        }
        document.documentElement.style.setProperty('--shell-h', Math.ceil(h) + 'px');
    }
    window.addEventListener('resize', syncShellHeight);
    if (window.ResizeObserver) {
        const ro = new ResizeObserver(syncShellHeight);
        for (const sel of ['.header', '#levelsStrip', '.tab-bar']) { const el = document.querySelector(sel); if (el) ro.observe(el); }
    }

    document.addEventListener('DOMContentLoaded', () => {
        syncShellHeight();
        initPriceChart();
        initGexProfile();               // needs the price chart: it shares its price axis
        initSmileChart();

        // Size the Plotly chart from its container: it is resized only while the container has a real
        // size (never while the tab is hidden, which draws it squashed).
        const chartObserver = new ResizeObserver(entries => {
            for (const e of entries) {
                const el = e.target;
                if (e.contentRect.width > 0 && e.contentRect.height > 0 && el._fullLayout) {
                    scheduleRender(`resize.${el.id}`, () => {
                        if (el.offsetWidth > 0 && el.offsetHeight > 0) Plotly.Plots.resize(el);
                    }, { heavy: true });
                }
            }
        });
        chartObserver.observe(document.getElementById('smileChart'));

        // Window resize only swaps the margins for mobile/desktop transitions
        window.addEventListener('resize', () => {
            Plotly.relayout('smileChart', { margin: mobileSmileMargin() });
            if (state.activeTab === 'chain') {
                reportChainViewportCenter(true);
            }
        });

        state.chainAgeTimer = setInterval(() => { updateChainUpdateAge(); refreshStaleMarks(); }, 1000);

        document.getElementById('chainBody').addEventListener('click', onChainBodyClick);

        const chainWrap = document.getElementById('chainTableWrap');
        if (chainWrap) {
            chainWrap.addEventListener('scroll', () => { reportChainViewportCenter(false); });
        }

        state.activeTab = loadActiveTab();
        switchTab(state.activeTab);

        connectWS();
    });
