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

    function mobileGexMargin() {
        return isMobile() ? { t: 28, r: 30, b: 32, l: 40 } : { t: 36, r: 60, b: 40, l: 60 };
    }
    function mobileSmileMargin() {
        return isMobile()
            ? { t: 20, r: 40, b: 20, l: 40 }
            : { t: 28, r: 60, b: 30, l: 60 };
    }
    function mobileAxisFontSize() { return isMobile() ? 9 : 11; }

    function gexLayout() {
        const { bg, grid, text } = chartTheme();
        return {
            paper_bgcolor: bg,
            plot_bgcolor: bg,
            margin: { t: 36, r: 60, b: 40, l: 60 },
            barmode: 'relative',
            xaxis: { color: text, gridcolor: grid, title: { text: 'Strike', font: { color: text, size: 11 } } },
            yaxis: { color: text, gridcolor: grid, title: { text: 'GEX ($)', font: { color: text, size: 11 } } },
            showlegend: true,
            legend: { x: 0.01, y: 0.99, font: { color: text, size: 11 }, bgcolor: 'rgba(0,0,0,0)' },
            shapes: [],
            annotations: [],
        };
    }

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

    function initGexChart() {
        const fs = mobileAxisFontSize();
        const base = gexLayout();
        const { c, text } = chartTheme();
        const layout = {
            ...base,
            margin: mobileGexMargin(),
            xaxis: { ...base.xaxis, title: { text: 'Strike', font: { color: text, size: fs } } },
            yaxis: { ...base.yaxis, title: { text: 'GEX ($)', font: { color: text, size: fs } } },
            legend: { ...base.legend, font: { color: text, size: fs } },
        };
        Plotly.newPlot('gexChart', [
            {
                x: [], y: [],
                type: 'bar',
                name: 'Call GEX',
                marker: { color: withAlpha(c.up, 0.5) },
            },
            {
                x: [], y: [],
                type: 'bar',
                name: 'Put GEX',
                marker: { color: withAlpha(c.down, 0.5) },
            },
            {
                x: [], y: [],
                type: 'scatter',
                mode: 'lines+markers',
                name: 'Net GEX',
                line: { color: c.alt, width: 1.5 },
                marker: { color: c.alt, size: 3 },
            },
        ], layout, {
            displayModeBar: false,      // resizing is the ResizeObserver's job (main.js), only while visible
        });
        state.gexChartReady = true;
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

    // Shapes and annotations of the GEX chart: spot line, key levels, Net GEX box. A spot-only
    // change redraws just these through Plotly.relayout (requestSpotLineRender).
    function gexOverlays(gexData) {
        const c = themeColors();
        // Vertical line at current spot
        const shapes = [];
        const annotations = [];

        // Use latest currentSpot (updated via status msgs) so the line moves in
        // real-time as ES moves, rather than waiting for the next chain fetch.
        const spotForLine = spotLevel(gexData);
        const spotLabel = state.esDerived
            ? `ES derived SPX: ${spotForLine}`
            : `SPX: ${spotForLine}`;

        if (spotForLine > 0) {
            shapes.push({
                type: 'line',
                xref: 'x', x0: spotForLine, x1: spotForLine,
                yref: 'paper', y0: 0, y1: 1,
                line: { color: c.textStrong, width: 2, dash: 'dot' },
            });
            annotations.push({
                x: spotForLine,
                yref: 'paper', y: 1.02,
                text: spotLabel,
                showarrow: false,
                font: { color: state.esDerived ? c.accent : c.textStrong, size: 10 },
            });
        }

        // Mark key strikes on GEX chart
        const keyLevels = [
            { val: gexData.call_wall, color: c.up, label: 'CW' },
            { val: gexData.put_wall, color: c.down, label: 'PW' },
            { val: gexData.gamma_flip, color: c.accent, label: 'GF' },
            { val: gexData.max_pain, color: c.info, label: 'MP' },
        ];

        for (const lv of keyLevels) {
            if (lv.val == null) continue;
            shapes.push({
                type: 'line',
                xref: 'x', x0: lv.val, x1: lv.val,
                yref: 'paper', y0: 0, y1: 1,
                line: { color: lv.color, width: 1.5, dash: 'dash' },
            });
            annotations.push({
                x: lv.val,
                yref: 'paper', y: -0.06,
                text: lv.label,
                showarrow: false,
                font: { color: lv.color, size: 10, },
            });
        }

        // Net GEX + regime annotation in top-right of chart
        const netGex = gexData.total_net_gex;
        if (netGex != null) {
            const isPositive = netGex >= 0;
            const regimeLabel = isPositive ? '- CONVERGING' : '- DIVERGING';
            const regimeColor = isPositive ? c.up : c.down;
            const callOI = gexData.total_call_oi;
            const putOI = gexData.total_put_oi;
            const callOIStr = (callOI != null && callOI > 0) ? _intFmt.format(callOI) : '-';
            const putOIStr = (putOI != null && putOI > 0) ? _intFmt.format(putOI) : '-';
            
            // P/C OI Ratio
            let pcRatioStr = '-';
            let pcRatioColor = c.textMuted;
            if (callOI > 0) {
                const pcRatio = putOI / callOI;
                pcRatioStr = pcRatio.toFixed(2);
                pcRatioColor = pcRatio <= 1 ? c.up : c.down;
            }
            
            // GEX Skew %
            let gexSkewStr = '-';
            let gexSkewColor = c.textMuted;
            const totalCallG = gexData.total_call_gex || 0;
            const totalPutG = Math.abs(gexData.total_put_gex || 0);
            const totalGross = totalCallG + totalPutG;
            if (totalGross > 0) {
                const pct = (totalCallG / totalGross * 100);
                gexSkewStr = pct.toFixed(0) + '%';
                gexSkewColor = pct >= 50 ? c.up : c.down;
            }
            
            annotations.push({
                xref: 'paper', x: 0.98,
                yref: 'paper', y: 0.99,
                text: `Net GEX: <b>${fmtGex(netGex)}</b>   <span style="color:${regimeColor}">${regimeLabel}</span><br>Call OI/Put OI: <span style="color:${c.up}"><b>${callOIStr}</b></span>/<span style="color:${c.down}"><b>${putOIStr}</b></span><br>P/C OI: <span style="color:${pcRatioColor}"><b>${pcRatioStr}</b></span>   Call GEX%: <span style="color:${gexSkewColor}"><b>${gexSkewStr}</b></span>`,
                showarrow: false,
                font: { color: c.textStrong, size: 12 },
                xanchor: 'right',
                yanchor: 'top',
                bgcolor: c.bgOverlay,
                bordercolor: isPositive ? c.up : c.down,
                borderwidth: 2,
                borderpad: 8,
                borderradius: 4,
            });
        }

        return { shapes, annotations };
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

    // Cheap change signature of what a full draw would paint. The spot is left out on purpose: a spot-only
    // change is the relayout path (requestSpotLine), which keeps the user's zoom.
    function gexSignature(g, view) {
        let h = view.length;
        for (const b of view) {
            h = (Math.imul(h, 31) + Math.round((b.call_gex || 0) / 1e3) * 7 + Math.round((b.put_gex || 0) / 1e3) * 13
                 + (b.strike | 0) + (b.call_oi | 0) * 3 + (b.put_oi | 0) * 5 + (b.call_vol | 0) * 11 + (b.put_vol | 0) * 17) | 0;
        }
        return [h, view.length, g.call_wall, g.put_wall, g.gamma_flip, g.max_pain, g.total_net_gex, g.total_call_oi, g.total_put_oi,
                state.esDerived ? 1 : 0, state.gexMode, state.gexShowAll ? 1 : 0, getTheme()].join('|');
    }

    function smileSignature(g, view) {
        let h = view.length;
        for (const d of view) {
            h = (Math.imul(h, 31) + (d.strike | 0) + Math.round((d.call_iv || 0) * 100) * 7 + Math.round((d.put_iv || 0) * 100) * 13
                 + Math.round((d.call_efficiency || 0) * 1e4) * 3 + Math.round((d.put_efficiency || 0) * 1e4) * 5) | 0;
        }
        return [h, view.length, g.call_wall, g.put_wall, g.gamma_flip, state.gexMode, state.gexShowAll ? 1 : 0, getTheme()].join('|');
    }

    let _gexSig = '', _smileSig = '', _smileDrawnAt = 0, _smileTimer = 0, _smileForce = false;

    function updateGexChart() {
        if (!state.gexChartReady) return;

        // Pick data source based on gex mode
        const gexData = currentGexData();
        if (!gexData || !gexData.gex_bars) return;

        const bars = windowItems(gexData.gex_bars, spotLevel(gexData));
        const sig = gexSignature(gexData, bars);
        if (sig === _gexSig) { requestSpotLine('gex'); return; }       // nothing new to paint: only the spot may have moved
        const strikes = bars.map(b => b.strike);
        const callGex = bars.map(b => b.call_gex);
        const putGex = bars.map(b => b.put_gex);
        const netGexPerBar = bars.map(b => b.net_gex);
        const { shapes, annotations } = gexOverlays(gexData);
        const base = gexLayout();
        const { c } = chartTheme();

        // Calculate common range from all smile data if available
        let commonRange = null;
        if (gexData && gexData.smile_data && gexData.smile_data.length > 0) {
            const smileStrikes = windowItems(gexData.smile_data, spotLevel(gexData)).map(d => d.strike).filter(s => s != null);
            if (smileStrikes.length > 0) {
                const minSmile = Math.min(...smileStrikes);
                const maxSmile = Math.max(...smileStrikes);
                // Add 1% padding
                const range = maxSmile - minSmile;
                commonRange = [minSmile - range * 0.01, maxSmile + range * 0.01];
            }
        }

        Plotly.react('gexChart', [
            {
                x: strikes,
                y: callGex,
                type: 'bar',
                name: 'Call GEX',
                marker: { color: strikes.map(() => withAlpha(c.up, 0.5)) },
                customdata: bars.map(b => [fmtGex(b.call_gex), _intFmt.format(b.call_oi ?? 0), _intFmt.format(b.call_vol ?? 0)]),
                hovertemplate: '<b>Strike: %{x}</b><br>Call GEX: %{customdata[0]}<br>Call OI: %{customdata[1]}<br>Call Vol: %{customdata[2]}<extra></extra>',
            },
            {
                x: strikes,
                y: putGex,
                type: 'bar',
                name: 'Put GEX',
                marker: { color: strikes.map(() => withAlpha(c.down, 0.5)) },
                customdata: bars.map(b => [fmtGex(b.put_gex), _intFmt.format(b.put_oi ?? 0), _intFmt.format(b.put_vol ?? 0)]),
                hovertemplate: '<b>Strike: %{x}</b><br>Put GEX: %{customdata[0]}<br>Put OI: %{customdata[1]}<br>Put Vol: %{customdata[2]}<extra></extra>',
            },
            {
                x: strikes,
                y: netGexPerBar,
                type: 'scatter',
                mode: 'lines+markers',
                name: 'Net GEX',
                line: { color: c.alt, width: 1.5 },
                marker: { color: c.alt, size: 3 },
                customdata: bars.map(b => [fmtGex(b.net_gex)]),
                hovertemplate: '<b>Strike: %{x}</b><br>Net GEX: %{customdata[0]}<extra></extra>',
            },
        ], {
            ...base,
            shapes,
            annotations,
            xaxis: { ...base.xaxis, range: commonRange },
        });
        _gexSig = sig;
        noteSpotDrawn('gex', gexData);
    }

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
    // Render requests: GEX and smile are drawn only while the Dashboard tab is visible (parked per
    // tab otherwise and drawn once on show). Both are heavy jobs: coalesced per key, one per frame, and
    // after the light work (price bars, chain cells) of the frame they were requested in.
    // ======================================================================
    function requestGexRender() {
        renderWhenVisible('dashboard', 'gex', updateGexChart, { heavy: true });
        renderWhenVisible('dashboard', 'smile', updateSmileChart, { heavy: true });
    }

    // Theme change: recolor both Plotly charts and the price chart. Runs as a heavy job and, like every
    // chart job, only while the Dashboard is visible (parked and run on show otherwise), so a flip made
    // on another tab shows the new colors on return. The relayout covers a chart that has no data yet;
    // the redraw recolors traces, overlays and axis titles.
    function patchChartTheme() {
        const { bg, grid, text } = chartTheme();
        const patch = { paper_bgcolor: bg, plot_bgcolor: bg, 'xaxis.color': text, 'xaxis.gridcolor': grid,
                        'yaxis.color': text, 'yaxis.gridcolor': grid, 'legend.font.color': text };
        for (const id of ['gexChart', 'smileChart']) {
            const el = document.getElementById(id);
            if (el && el._fullLayout) Plotly.relayout(id, patch);
        }
        _smileForce = true;
        requestGexRender();
    }
    window.addEventListener('themechange', () => {
        renderWhenVisible('dashboard', 'chart.theme', patchChartTheme, { heavy: true });
    });

    // A spot-only change moves the spot lines with a relayout of the shapes and annotations (the
    // charts' data is not rebuilt, the user's zoom stays). Each chart has its own heavy job, so the two
    // never share a frame, and at most one relayout per chart per state.spotLineMinIntervalMs: a change
    // inside the window arms one timer for the window's end, and the job then draws the latest spot.
    // What each chart last drew (full draw or relayout) decides when a relayout would paint nothing new.
    const _spotDrawn = { gex: null, smile: null };       // {data, spot, es, at}
    const _spotTimers = { gex: 0, smile: 0 };

    function noteSpotDrawn(kind, gexData) {
        _spotDrawn[kind] = { data: gexData, spot: spotLevel(gexData), es: !!state.esDerived, at: performance.now() };
    }

    function requestSpotLineRender() {
        requestSpotLine('gex');
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
        if (kind === 'gex') {
            if (!g.gex_bars) return;
            Plotly.relayout('gexChart', gexOverlays(g));
        } else {
            if (!(g.smile_data && g.smile_data.length)) return;
            Plotly.relayout('smileChart', smileOverlays(g));
        }
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

        // Update chart labels
        const gexLabel = document.getElementById('gexChartLabel');
        const smileLabel = document.getElementById('smileChartLabel');
        if (state.gexMode === 'monthly') {
            const expStr = state.monthlyExpiration || '';
            gexLabel.textContent = `Gamma Exposure (GEX) — SPX Monthly${expStr ? ' ' + expStr : ''}`;
            smileLabel.textContent = `IV Smile — SPX Monthly${expStr ? ' ' + expStr : ''}`;
        } else {
            gexLabel.textContent = 'Gamma Exposure (GEX) by Strike';
            smileLabel.textContent = 'IV Smile & Delta-Decay Efficiency';
        }
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
