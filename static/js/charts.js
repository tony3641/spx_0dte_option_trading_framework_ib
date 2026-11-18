    // Chart setup
    // ======================================================================
    // Chart colors come from the theme tokens, read at draw time (theme-colors.js).
    function chartTheme() {
        const c = themeColors();
        return { bg: c.bgPanel, grid: c.border, text: c.textMuted, c };
    }

    // ======================================================================
    // Responsive helpers
    // ======================================================================
    function isMobile() { return window.innerWidth < 600; }

    function mobileSmileMargin() {
        return isMobile()
            ? { t: 20, r: 40, b: 20, l: 40 }
            : { t: 28, r: 60, b: 30, l: 60 };
    }
    function mobileAxisFontSize() { return isMobile() ? 9 : 11; }

    function smileLayout(range) {
        const { bg, grid, text, c } = chartTheme();
        const fs = mobileAxisFontSize();
        const ax = (extra) => Object.assign({ color: text, gridcolor: grid }, extra);
        const none = 'rgba(0,0,0,0)';
        const rng = range ? { range } : {};
        return {
            paper_bgcolor: bg,
            plot_bgcolor: bg,
            margin: mobileSmileMargin(),
            showlegend: true,
            legend: { x: 0.01, y: 1.0, font: { color: text, size: fs }, bgcolor: 'rgba(0,0,0,0)', orientation: 'h' },
            grid: { rows: 2, columns: 1, subplots: [['xy'], ['x2y3']], roworder: 'top to bottom', ygap: 0.12 },
            xaxis:  ax(Object.assign({ showticklabels: false, matches: 'x2' }, rng)),
            yaxis:  ax({ title: { text: 'Call IV %', font: { color: c.up, size: fs } }, side: 'left' }),
            yaxis2: ax({ gridcolor: none, title: { text: 'Efficiency', font: { color: c.alt, size: fs } }, side: 'right', overlaying: 'y', showgrid: false }),
            xaxis2: ax(Object.assign({ title: { text: 'Strike', font: { color: text, size: fs } } }, rng)),
            yaxis3: ax({ title: { text: 'Put IV %', font: { color: c.down, size: fs } }, side: 'left' }),
            yaxis4: ax({ gridcolor: none, title: { text: 'Efficiency', font: { color: c.alt, size: fs } }, side: 'right', overlaying: 'y3', showgrid: false }),
        };
    }

    function initSmileChart() {
        const { c } = chartTheme();
        const layout = Object.assign(smileLayout(null), {
            annotations: [
                { text: 'CALLS', xref: 'paper', yref: 'paper', xanchor: 'right', x: 0.99, y: 1.01, showarrow: false, font: { color: c.up, size: 11, weight: 'bold' } },
                { text: 'PUTS',  xref: 'paper', yref: 'paper', xanchor: 'right', x: 0.99, y: 0.46, showarrow: false, font: { color: c.down, size: 11, weight: 'bold' } },
            ],
            shapes: [],
        });
        // 4 traces:  call IV, call eff, put IV, put eff
        Plotly.newPlot('smileChart', [
            { x: [], y: [], type: 'scatter', mode: 'lines', name: 'Call IV', line: { color: c.up, width: 2 }, xaxis: 'x', yaxis: 'y' },
            { x: [], y: [], type: 'scatter', mode: 'lines', name: 'Call Eff', line: { color: c.alt, width: 1.5, dash: 'dot' }, xaxis: 'x', yaxis: 'y2' },
            { x: [], y: [], type: 'scatter', mode: 'lines', name: 'Put IV', line: { color: c.down, width: 2 }, xaxis: 'x2', yaxis: 'y3' },
            { x: [], y: [], type: 'scatter', mode: 'lines', name: 'Put Eff', line: { color: c.alt, width: 1.5, dash: 'dot' }, xaxis: 'x2', yaxis: 'y4' },
        ], layout, { displayModeBar: false });    // resized by main.js's ResizeObserver, only while visible
        state.smileChartReady = true;
    }

    // ======================================================================
    // Chart update functions
    // ======================================================================
    function handleChainProgress(data) {
        const gexOverlay  = document.getElementById('gexLoading');
        const gexTextEl   = document.getElementById('gexLoadingText');
        const gexSubEl    = document.getElementById('gexLoadingSub');
        const smileOverlay = document.getElementById('smileLoading');
        const smileTextEl  = document.getElementById('smileLoadingText');
        const smileSubEl   = document.getElementById('smileLoadingSub');

        if (data.phase === 'done') {
            gexOverlay.classList.add('hidden');
            smileOverlay.classList.add('hidden');
            return;
        }

        // Only show during the very first fetch - suppress spinner for recurring updates
        if (state.gex) return;

        gexOverlay.classList.remove('hidden');
        smileOverlay.classList.remove('hidden');

        const phaseText = {
            starting:   'Fetching option chain-',
            qualifying: 'Qualifying contracts-',
            fetching:   'Streaming market data-',
            computing:  'Computing GEX-',
        };
        const text = phaseText[data.phase] || 'Fetching option chain-';
        gexTextEl.textContent = text;
        smileTextEl.textContent = text;

        if (data.phase === 'fetching') {
            const subText = `Batch ${data.batch} of ${data.total_batches}`;
            gexSubEl.textContent = subText;
            smileSubEl.textContent = subText;
        } else {
            gexSubEl.textContent = '';
            smileSubEl.textContent = '';
        }
    }

    // The GEX data the Dashboard charts show: 0DTE, or the cached monthly set in monthly mode.
    function currentGexData() {
        return state.gexMode === 'monthly' ? state.monthlyGex : state.gex;
    }

    // Number.prototype.toLocaleString() builds a formatter per call (about 480 calls per GEX draw);
    // one shared formatter gives the same text for 1/4 of the cost.
    const _intFmt = new Intl.NumberFormat();

    // The spot the lines are drawn at: the live one (status messages) once known, else the data's own.
    function spotLevel(gexData) {
        return state.currentSpot > 0 ? state.currentSpot : (gexData.spot_price || 0);
    }

    // The strikes drawn: within state.gexWindowStrikes steps of spot (the step is the median gap between
    // neighbouring strikes). null = no window (spot unknown, few strikes, or "All" selected).
    function strikeWindow(strikes, spot, n) {
        if (!(spot > 0) || !(n > 0) || strikes.length <= 2 * n + 1) return null;
        const sorted = strikes.filter(s => s != null).sort((a, b) => a - b);
        const gaps = [];
        for (let i = 1; i < sorted.length; i++) { const g = sorted[i] - sorted[i - 1]; if (g > 0) gaps.push(g); }
        if (!gaps.length) return null;
        gaps.sort((a, b) => a - b);
        const step = gaps[Math.floor(gaps.length / 2)];
        const lo = spot - n * step, hi = spot + n * step;
        return (sorted[0] >= lo && sorted[sorted.length - 1] <= hi) ? null : [lo, hi];
    }

    // items = [{strike, ...}]; a window that selects nothing (spot far outside the data) shows everything.
    function windowItems(items, spot) {
        if (state.gexShowAll) return items;
        const w = strikeWindow(items.map(i => i.strike), spot, state.gexWindowStrikes);
        if (!w) return items;
        const out = items.filter(i => i.strike >= w[0] && i.strike <= w[1]);
        return out.length ? out : items;
    }

    function smileSignature(g, view) {
        let h = view.length;
        for (const d of view) {
            h = (Math.imul(h, 31) + (d.strike | 0) + Math.round((d.call_iv || 0) * 100) * 7 + Math.round((d.put_iv || 0) * 100) * 13
                 + Math.round((d.call_efficiency || 0) * 1e4) * 3 + Math.round((d.put_efficiency || 0) * 1e4) * 5) | 0;
        }
        return [h, view.length, g.call_wall, g.put_wall, g.gamma_flip, state.gexMode, state.gexShowAll ? 1 : 0, getTheme()].join('|');
    }

    let _smileSig = '', _smileDrawnAt = 0, _smileTimer = 0, _smileForce = false;

    // Shapes and annotations of the smile chart: the spot and level verticals on both subplots and
    // the CALLS / PUTS subtitles.
    function smileOverlays(gexData) {
        const c = themeColors();
        // Spot + key level vertical lines for both subplots
        const shapes = [];
        const spotForLine = spotLevel(gexData);
        const addVertical = (xref, val, color, dash) => {
            if (val == null || val <= 0) return;
            shapes.push({
                type: 'line', xref: xref, x0: val, x1: val,
                yref: 'paper', y0: 0, y1: 1,
                line: { color: color, width: 1, dash: dash },
            });
        };
        // Draw on both x-axes
        for (const xr of ['x', 'x2']) {
            addVertical(xr, spotForLine, c.textStrong, 'dot');
            addVertical(xr, gexData.call_wall, c.up, 'dash');
            addVertical(xr, gexData.put_wall, c.down, 'dash');
            addVertical(xr, gexData.gamma_flip, c.accent, 'dash');
        }

        // Preserve the CALLS / PUTS subtitle annotations
        const annotations = [
            { text: 'CALLS', xref: 'paper', yref: 'paper', xanchor: 'right', x: 0.99, y: 1.01, showarrow: false, font: { color: c.up, size: 11 } },
            { text: 'PUTS',  xref: 'paper', yref: 'paper', xanchor: 'right', x: 0.99, y: 0.46, showarrow: false, font: { color: c.down, size: 11 } },
        ];
        return { shapes, annotations };
    }

    function updateSmileChart() {
        if (!state.smileChartReady) return;

        // Pick data source based on gex mode
        const gexData = currentGexData();
        if (!gexData || !gexData.smile_data) return;

        const sdAll = gexData.smile_data;
        if (sdAll.length === 0) return;
        const sd = windowItems(sdAll, spotLevel(gexData));

        const sig = smileSignature(gexData, sd);
        if (sig === _smileSig) { requestSpotLine('smile'); return; }
        const wait = _smileDrawnAt + state.smileMinIntervalMs - performance.now();
        if (!_smileForce && _smileDrawnAt && wait > 0) {           // the smile moves slowly: coalesce its redraws
            if (!_smileTimer) {
                _smileTimer = setTimeout(() => {
                    _smileTimer = 0;
                    renderWhenVisible('dashboard', 'smile', updateSmileChart, { heavy: true });
                }, wait);
            }
            requestSpotLine('smile');
            return;
        }
        _smileForce = false;

        // Separate call and put data (filter nulls)
        const callStrikes = [], callIV = [], callEff = [], callCustom = [];
        const putStrikes  = [], putIV  = [], putEff  = [], putCustom = [];

        for (const d of sd) {
            if (d.call_iv != null) {
                callStrikes.push(d.strike);
                callIV.push(d.call_iv);
                callEff.push(d.call_efficiency);
                callCustom.push([
                    d.call_delta != null ? d.call_delta.toFixed(4) : '-',
                    d.call_charm != null ? d.call_charm.toFixed(6) : '-',
                    d.call_efficiency != null ? d.call_efficiency.toFixed(4) : '-',
                ]);
            }
            if (d.put_iv != null) {
                putStrikes.push(d.strike);
                putIV.push(d.put_iv);
                putEff.push(d.put_efficiency);
                putCustom.push([
                    d.put_delta != null ? d.put_delta.toFixed(4) : '-',
                    d.put_charm != null ? d.put_charm.toFixed(6) : '-',
                    d.put_efficiency != null ? d.put_efficiency.toFixed(4) : '-',
                ]);
            }
        }

        // Calculate common range from all smile data
        let commonRange = null;
        const smileStrikes = sd.map(d => d.strike).filter(s => s != null);
        if (smileStrikes.length > 0) {
            const minSmile = Math.min(...smileStrikes);
            const maxSmile = Math.max(...smileStrikes);
            // Add 1% padding
            const range = maxSmile - minSmile;
            commonRange = [minSmile - range * 0.01, maxSmile + range * 0.01];
        }

        const { shapes, annotations } = smileOverlays(gexData);
        const { c } = chartTheme();

        const hoverCall = '<b>Strike: %{x}</b><br>Call IV: %{y:.1f}%<br>Delta: %{customdata[0]}<br>Charm: %{customdata[1]}<br>Efficiency: %{customdata[2]}<extra></extra>';
        const hoverCallEff = '<b>Strike: %{x}</b><br>Efficiency: %{y:.4f}<extra></extra>';
        const hoverPut = '<b>Strike: %{x}</b><br>Put IV: %{y:.1f}%<br>Delta: %{customdata[0]}<br>Charm: %{customdata[1]}<br>Efficiency: %{customdata[2]}<extra></extra>';
        const hoverPutEff = '<b>Strike: %{x}</b><br>Efficiency: %{y:.4f}<extra></extra>';

        Plotly.react('smileChart', [
            { x: callStrikes, y: callIV, type: 'scatter', mode: 'lines', name: 'Call IV',
              line: { color: c.up, width: 2 }, xaxis: 'x', yaxis: 'y',
              customdata: callCustom, hovertemplate: hoverCall },
            { x: callStrikes, y: callEff, type: 'scatter', mode: 'lines', name: 'Call Eff',
              line: { color: c.alt, width: 1.5, dash: 'dot' }, xaxis: 'x', yaxis: 'y2',
              hovertemplate: hoverCallEff },
            { x: putStrikes, y: putIV, type: 'scatter', mode: 'lines', name: 'Put IV',
              line: { color: c.down, width: 2 }, xaxis: 'x2', yaxis: 'y3',
              customdata: putCustom, hovertemplate: hoverPut },
            { x: putStrikes, y: putEff, type: 'scatter', mode: 'lines', name: 'Put Eff',
              line: { color: c.alt, width: 1.5, dash: 'dot' }, xaxis: 'x2', yaxis: 'y4',
              hovertemplate: hoverPutEff },
        ], Object.assign(smileLayout(commonRange), {
            shapes,
            annotations,
        }));
        _smileSig = sig;
        _smileDrawnAt = performance.now();
        noteSpotDrawn('smile', gexData);
    }

    // ======================================================================
    // Render requests: the GEX profile (a canvas beside the price chart, gex-profile.js) and the smile
    // are drawn only while the Dashboard tab is visible (parked per tab otherwise and drawn once on
    // show). The profile is a cheap light job; the smile is a heavy one: coalesced per key, one per
    // frame, and after the light work (price bars, chain cells) of the frame it was requested in.
    // ======================================================================
    function requestGexRender() {
        renderWhenVisible('dashboard', 'gex', updateGexProfile);
        renderWhenVisible('dashboard', 'smile', updateSmileChart, { heavy: true });
    }

    // Theme change: recolor the smile, the profile and the price chart. Runs as a heavy job and, like
    // every chart job, only while the Dashboard is visible (parked and run on show otherwise), so a flip
    // made on another tab shows the new colors on return. The relayout covers a chart that has no data
    // yet; the redraw recolors traces, overlays and axis titles.
    function patchChartTheme() {
        const { bg, grid, text } = chartTheme();
        const patch = { paper_bgcolor: bg, plot_bgcolor: bg, 'xaxis.color': text, 'xaxis.gridcolor': grid,
                        'yaxis.color': text, 'yaxis.gridcolor': grid, 'legend.font.color': text };
        const smile = document.getElementById('smileChart');
        if (smile && smile._fullLayout) Plotly.relayout('smileChart', patch);
        _smileForce = true;
        requestGexRender();
    }
    window.addEventListener('themechange', () => {
        renderWhenVisible('dashboard', 'chart.theme', patchChartTheme, { heavy: true });
    });

    // A spot-only change moves the smile's spot lines with a relayout of the shapes (the chart's data is
    // not rebuilt, the user's zoom stays): at most one relayout per state.spotLineMinIntervalMs, a change
    // inside the window arms one timer for the window's end, and the job then draws the latest spot.
    // What the smile last drew (full draw or relayout) decides when a relayout would paint nothing new.
    // The GEX profile is cheap and just repaints at the new spot.
    const _spotDrawn = { smile: null };                  // {data, spot, es, at}
    const _spotTimers = { smile: 0 };

    function noteSpotDrawn(kind, gexData) {
        _spotDrawn[kind] = { data: gexData, spot: spotLevel(gexData), es: !!state.esDerived, at: performance.now() };
    }

    function requestSpotLineRender() {
        renderWhenVisible('dashboard', 'gex.spot', drawGexProfile);
        requestSpotLine('smile');
    }

    function requestSpotLine(kind) {
        if (_spotTimers[kind]) return;                    // the trailing edge is armed: it draws the latest spot
        const drawn = _spotDrawn[kind];
        if (!drawn) return;                               // never drawn: the first full draw paints the spot line
        const wait = drawn.at + state.spotLineMinIntervalMs - performance.now();
        if (wait > 0) {
            _spotTimers[kind] = setTimeout(() => { _spotTimers[kind] = 0; requestSpotLine(kind); }, wait);
            return;
        }
        renderWhenVisible('dashboard', `${kind}.spot`, () => redrawSpotLine(kind), { heavy: true });
    }

    function redrawSpotLine(kind) {
        const g = currentGexData();
        const drawn = _spotDrawn[kind];
        if (!g || !drawn) return;
        if (isRenderPending(kind)) return;                // a full draw is queued and paints the current spot
        if (drawn.data === g && drawn.spot === spotLevel(g) && drawn.es === !!state.esDerived) return;   // nothing new
        if (!(g.smile_data && g.smile_data.length)) return;
        Plotly.relayout('smileChart', smileOverlays(g));
        noteSpotDrawn(kind, g);
    }

    // ======================================================================
    // GEX mode toggle (0DTE ↔ Monthly)
    // ======================================================================
    function setGexMode(mode) {
        if (mode !== '0dte' && mode !== 'monthly') return;
        if (mode === state.gexMode) return;

        state.gexMode = mode;
        updateGexModeToggle();

        // Notify server
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(`set_gex_mode:${mode}`);
        }

        // Re-render charts with the active data source
        _smileForce = true;
        requestGexRender();
    }

    function setGexRange(all) {
        if (!!all === state.gexShowAll) return;
        state.gexShowAll = !!all;
        document.querySelectorAll('#gexRangeToggle .gex-mode-btn').forEach(b => {
            b.classList.toggle('active', (b.dataset.range === 'all') === state.gexShowAll);
        });
        _smileForce = true;
        requestGexRender();
    }

    function updateGexModeToggle() {
        const btns = document.querySelectorAll('#gexModeToggle .gex-mode-btn');
        btns.forEach(btn => {
            btn.classList.toggle('active', btn.dataset.mode === state.gexMode);
        });

        // Update chart labels (the profile writes its own expiration label with its summary)
        const smileLabel = document.getElementById('smileChartLabel');
        if (state.gexMode === 'monthly') {
            const expStr = state.monthlyExpiration || '';
            smileLabel.textContent = `IV Smile — SPX Monthly${expStr ? ' ' + expStr : ''}`;
        } else {
            smileLabel.textContent = 'IV Smile & Delta-Decay Efficiency';
        }
        renderWhenVisible('dashboard', 'gex', updateGexProfile);
    }

    function handleMonthlyGexProgress(data) {
        const gexOverlay = document.getElementById('gexLoading');
        const gexTextEl = document.getElementById('gexLoadingText');
        const gexSubEl = document.getElementById('gexLoadingSub');
        const smileOverlay = document.getElementById('smileLoading');
        const smileTextEl = document.getElementById('smileLoadingText');
        const smileSubEl = document.getElementById('smileLoadingSub');

        if (data.phase === 'done') {
            gexOverlay.classList.add('hidden');
            smileOverlay.classList.add('hidden');
            return;
        }

        // Only show spinner if we're in monthly mode and don't have data yet
        if (state.gexMode !== 'monthly') return;
        if (state.monthlyGex) return;

        gexOverlay.classList.remove('hidden');
        smileOverlay.classList.remove('hidden');
        gexTextEl.textContent = 'Fetching monthly option chain\u2026';
        gexSubEl.textContent = '';
        smileTextEl.textContent = 'Fetching monthly option chain\u2026';
        smileSubEl.textContent = '';
    }

    // ======================================================================
