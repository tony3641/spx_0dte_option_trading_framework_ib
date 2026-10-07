    // Init
    // ======================================================================
    document.addEventListener('DOMContentLoaded', () => {
        initPriceChart();
        initGexChart();
        initSmileChart();

        // Sync horizontal zoom between GEX chart (xaxis) and Smile chart (xaxis + xaxis2)
        // Guard flag prevents the two handlers from triggering each other infinitely.
        let syncingZoom = false;

        document.getElementById('gexChart').on('plotly_relayout', (data) => {
            if (syncingZoom) return;
            if (data['xaxis.range[0]'] !== undefined && data['xaxis.range[1]'] !== undefined) {
                const xRange = [data['xaxis.range[0]'], data['xaxis.range[1]']];
                syncingZoom = true;
                Plotly.relayout('smileChart', {
                    'xaxis.range': xRange,
                    'xaxis2.range': xRange,
                }).finally(() => { syncingZoom = false; });
            } else if (data['xaxis.autorange'] !== undefined) {
                syncingZoom = true;
                Plotly.relayout('smileChart', {
                    'xaxis.autorange': data['xaxis.autorange'],
                    'xaxis2.autorange': data['xaxis.autorange'],
                }).finally(() => { syncingZoom = false; });
            }
        });

        document.getElementById('smileChart').on('plotly_relayout', (data) => {
            if (syncingZoom) return;
            if (data['xaxis.range[0]'] !== undefined && data['xaxis.range[1]'] !== undefined) {
                const xRange = [data['xaxis.range[0]'], data['xaxis.range[1]']];
                syncingZoom = true;
                Plotly.relayout('gexChart', { 'xaxis.range': xRange })
                    .finally(() => { syncingZoom = false; });
            } else if (data['xaxis.autorange'] !== undefined) {
                syncingZoom = true;
                Plotly.relayout('gexChart', { 'xaxis.autorange': data['xaxis.autorange'] })
                    .finally(() => { syncingZoom = false; });
            }
        });

        // Size the Plotly charts from their containers: a chart is resized only while its container has
        // a real size (never while the tab is hidden, which draws it squashed), one chart per frame.
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
        ['gexChart', 'smileChart'].forEach(id => chartObserver.observe(document.getElementById(id)));

        // Window resize only swaps the margins and font sizes for mobile/desktop transitions
        window.addEventListener('resize', () => {
            const fs = mobileAxisFontSize();
            Plotly.relayout('gexChart', {
                margin: mobileGexMargin(),
                'xaxis.title.font.size': fs,
                'yaxis.title.font.size': fs,
                'legend.font.size': fs,
            });
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
