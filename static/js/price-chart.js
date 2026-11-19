    // SPX price chart (TradingView Lightweight Charts). Times are ET wall-clock encoded as UTC
    // seconds, so the axis shows ET whatever the PC's time zone.
    // ======================================================================
    // Each level is a dotted step line: it starts at the first bar (or when the level first appeared),
    // moves up or down at the minute GEX published a different value, and runs on to the latest bar.
    // style: 'dotted' (default) | 'dashed' | 'solid'; width defaults to 2; alpha to LEVEL_ALPHA; profile: false
    // keeps the entry off the GEX profile (the profile draws the expected move and the pins its own way).
    const PRICE_LEVELS = [
        { key: 'call_wall', color: 'up', name: 'Call Wall', tag: 'CW' },
        { key: 'put_wall', color: 'down', name: 'Put Wall', tag: 'PW' },
        { key: 'gamma_flip', color: 'accent', name: 'Zero Gamma', tag: 'ZG' },
        { key: 'max_pain', color: 'info', name: 'Max Pain', tag: 'MP' },
        { key: 'em_high', color: 'textFaint', name: 'Expected move high', tag: 'EM+', style: 'dashed', width: 1, profile: false },
        { key: 'em_low', color: 'textFaint', name: 'Expected move low', tag: 'EM-', style: 'dashed', width: 1, profile: false },
        { key: 'node_r1', color: 'down', name: 'Nearest pin above', tag: 'R', style: 'solid', width: 1, alpha: 0.45, profile: false },
        { key: 'node_s1', color: 'up', name: 'Nearest pin below', tag: 'S', style: 'solid', width: 1, alpha: 0.45, profile: false },
    ];
    // The axis tag of a level is its two-letter code, and the line and tag are translucent so the candles
    // behind them stay readable (the full names are in the "?" help and the Levels strip).
    const LEVEL_ALPHA = 0.7;
    const levelColor = (c, lv) => withAlpha(c[lv.color], lv.alpha ?? LEVEL_ALPHA);
    const LEVEL_LINE_STYLE = { dotted: 'Dotted', dashed: 'Dashed', solid: 'Solid' };
    const LEVEL_ROWS_MAX = 2000;
    const CHARM_FRAMES_MAX = 1440;
    const CHARM_PREF_KEY = 'spx-charm-heatmap';          // localStorage '1' | '0'; default off
    const FLOW_PANE_KEY = 'spx-flow-pane';               // localStorage '1' | '0'; the hedge-flow pane, default on
    const SIGNAL_MARKERS_KEY = 'spx-signal-markers';     // localStorage '1' | '0'; the signal markers, default off
    const SIGNALS_MAX = 500;                             // the server's history size (signals.MAX_SIGNALS)
    // The model (bars, overnightPts, latest) is always current; the series are brought up to date
    // only while the Dashboard tab is visible. pending* hold what the next frame must push.
    const RSI_PERIOD = 14;
    const priceChart = {
        chart: null, candles: null, overnight: null, rsi: null, rsiGuides: [],
        sessionDate: null, bars: new Map(), overnightPts: new Map(), latest: null,
        pendingBars: new Map(), pendingOvernight: [],
        lastTime: null, lastOvernightTime: null,      // last time written to each series
        levelSeries: {}, levelRows: [], levelExpiration: null,   // rows: one per change, sorted by time
        charmFrames: [], charmVersion: 0, charmPrimitive: null, charmOn: false,   // frames: one per minute
        flowMinutes: new Map(), flowOn: true, flowSeries: null, flowCum: null,   // minutes by time; the pane is pane 2
        signals: [], signalsOn: false, signalMarkers: null,   // signal records (kept while the markers are off)
        needsFit: false, hadSnapshot: false,
    };

    function etIsoToChartTime(iso) {
        const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(iso || '');
        if (!m) return null;
        return Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]) / 1000;
    }

    // Wilder's RSI of ``closes``: null for the first ``period`` closes (warm-up), then
    // 100 - 100 / (1 + avgGain / avgLoss) with Wilder's smoothing. A flat market reads 50.
    function rsiValues(closes, period) {
        const out = new Array(closes.length).fill(null);
        if (closes.length <= period) return out;
        const rsi = (g, l) => (l === 0 ? (g === 0 ? 50 : 100) : 100 - 100 / (1 + g / l));
        let gain = 0, loss = 0;
        for (let i = 1; i <= period; i++) {
            const d = closes[i] - closes[i - 1];
            if (d > 0) gain += d; else loss -= d;
        }
        gain /= period;
        loss /= period;
        out[period] = rsi(gain, loss);
        for (let i = period + 1; i < closes.length; i++) {
            const d = closes[i] - closes[i - 1];
            gain = (gain * (period - 1) + Math.max(d, 0)) / period;
            loss = (loss * (period - 1) + Math.max(-d, 0)) / period;
            out[i] = rsi(gain, loss);
        }
        return out;
    }

    // RSI points for the bars (sorted by time); the warm-up bars have none.
    function _rsiPoints(candles) {
        const vals = rsiValues(candles.map(c => c.close), RSI_PERIOD);
        const pts = [];
        for (let i = 0; i < candles.length; i++) {
            if (vals[i] !== null) pts.push({ time: candles[i].time, value: vals[i] });
        }
        return pts;
    }

    function _toCandle(b) {
        return { time: etIsoToChartTime(b.time), open: b.open, high: b.high, low: b.low, close: b.close };
    }

    // A missing library (CDN down) or a chart that fails to build leaves the price chart area with a
    // short error text and priceChart.candles null (every render path checks it); the rest of the page
    // starts normally, so the caller (main.js) must not see an exception from here.
    function initPriceChart() {
        const el = document.getElementById('priceChart');
        let chart = null;
        try {
            if (typeof LightweightCharts === 'undefined') throw new Error('LightweightCharts is not defined');
            const LC = LightweightCharts;
            const { bg, grid, text, c } = chartTheme();
            chart = LC.createChart(el, {
                autoSize: true,
                layout: { background: { type: 'solid', color: bg }, textColor: text },
                grid: { vertLines: { color: grid }, horzLines: { color: grid } },
                timeScale: { timeVisible: true, secondsVisible: false, borderColor: grid },
                rightPriceScale: { borderColor: grid },
                crosshair: { mode: LC.CrosshairMode.Normal },
            });
            const candles = chart.addSeries(LC.CandlestickSeries, {
                upColor: c.up, downColor: c.down, borderVisible: false,
                wickUpColor: c.up, wickDownColor: c.down,
            });
            const overnight = chart.addSeries(LC.LineSeries, {
                color: c.accent, lineWidth: 1, lineStyle: LC.LineStyle.Dotted,
                priceLineVisible: false, lastValueVisible: true, title: 'ES-derived',
            });
            const charmPrimitive = new CharmHeatmapPrimitive(_charmColors);
            candles.attachPrimitive(charmPrimitive);          // painted behind the candles
            const levelSeries = {};
            for (const lv of PRICE_LEVELS) {
                levelSeries[lv.key] = chart.addSeries(LC.LineSeries, {
                    color: levelColor(c, lv), lineWidth: lv.width ?? 2,
                    lineStyle: LC.LineStyle[LEVEL_LINE_STYLE[lv.style] || 'Dotted'], lineType: LC.LineType.WithSteps,
                    priceLineVisible: false, lastValueVisible: true, crosshairMarkerVisible: false, title: lv.tag,
                    autoscaleInfoProvider: () => null,       // a far wall must not squash the candles
                });
            }
            // RSI(14) of the 1-minute closes in a pane under the price (same time axis), 70 / 30 guides.
            const rsi = chart.addSeries(LC.LineSeries, {
                color: c.alt, lineWidth: 1, priceLineVisible: false, lastValueVisible: true, title: 'RSI 14',
                priceFormat: { type: 'price', precision: 1, minMove: 0.1 },
                autoscaleInfoProvider: () => ({ priceRange: { minValue: 0, maxValue: 100 } }),
            }, 1);
            // Stretch factors 3:1:1 (price, RSI, hedge flow): the price pane takes 60% of the height and each of the
            // two strips below it 20%. The library's own default factors are not 1:1, so they are set here.
            chart.panes()[0].setStretchFactor(3);
            chart.panes()[1].setStretchFactor(1);
            const rsiGuides = [70, 30].map(price => rsi.createPriceLine({
                price, color: c.textFaint, lineWidth: 1, lineStyle: LC.LineStyle.Dashed, axisLabelVisible: false,
            }));
            const signalMarkers = LC.createSeriesMarkers(candles, []);   // v5 markers plugin, empty until switched on
            priceChart.chart = chart;                       // published together: never a half-built chart
            priceChart.candles = candles;
            priceChart.overnight = overnight;
            priceChart.levelSeries = levelSeries;
            priceChart.charmPrimitive = charmPrimitive;
            priceChart.rsi = rsi;
            priceChart.rsiGuides = rsiGuides;
            priceChart.signalMarkers = signalMarkers;
            let saved = null;
            try { saved = localStorage.getItem(CHARM_PREF_KEY); } catch (e) { /* storage blocked: stays off */ }
            setCharmHeatmap(saved === '1');
            let flowSaved = null;
            try { flowSaved = localStorage.getItem(FLOW_PANE_KEY); } catch (e) { /* storage blocked: default on */ }
            priceChart.flowOn = flowSaved !== '0';
            if (priceChart.flowOn) _tryAddFlowPane();
            _syncFlowToggle();
            let signalSaved = null;
            try { signalSaved = localStorage.getItem(SIGNAL_MARKERS_KEY); } catch (e) { /* storage blocked: stays off */ }
            priceChart.signalsOn = signalSaved === '1';
            _syncSignalToggle();
            state.priceChartReady = true;
        } catch (e) {
            console.error('Price chart unavailable', e);
            try { if (chart) chart.remove(); } catch (e2) { /* nothing left to clean up */ }
            el.textContent = 'Price chart library failed to load (check the network connection).';
            el.classList.add('price-chart-error');
        }
    }

    function handlePriceSnapshot(data) {
        if (!data) return;
        const prevSession = priceChart.sessionDate;
        priceChart.sessionDate = data.session_date || null;
        priceChart.bars = new Map();
        priceChart.latest = null;
        for (const b of (data.bars || [])) {
            const c = _toCandle(b);
            if (c.time === null) continue;
            priceChart.bars.set(c.time, c);
            if (!priceChart.latest || c.time >= priceChart.latest.time) priceChart.latest = c;
        }
        priceChart.overnightPts = new Map();                 // by time, last wins (setData and update agree)
        for (const p of (data.overnight || [])) {
            const t = etIsoToChartTime(p.time);
            if (t !== null) priceChart.overnightPts.set(t, { time: t, value: p.value });
        }
        priceChart.pendingBars.clear();
        priceChart.pendingOvernight = [];
        priceChart.levelRows = [];                           // the snapshot's history replaces the model
        priceChart.levelExpiration = null;
        for (const p of (data.levels || [])) recordLevelRow(_levelRow(p));
        priceChart.charmFrames = [];
        priceChart.charmVersion++;
        let charmStrikes = null;                             // a frame without strikes reuses the previous frame's
        for (const f of (data.charm || [])) {
            if (Array.isArray(f.strikes)) charmStrikes = f.strikes;
            recordCharmFrame(_charmFrame(Array.isArray(f.strikes) ? f : { ...f, strikes: charmStrikes }));
        }
        priceChart.flowMinutes = new Map();                  // the snapshot's flow series replaces the model
        for (const m of (data.flow || [])) recordFlowMinute(m);
        priceChart.signals = (data.signals || []).slice(-SIGNALS_MAX);   // the snapshot's history replaces the model
        // Fit only the first snapshot and a new session: a reconnect snapshot of the same session keeps the zoom.
        priceChart.needsFit = priceChart.needsFit || !priceChart.hadSnapshot || prevSession !== priceChart.sessionDate;
        priceChart.hadSnapshot = true;
        renderWhenVisible('dashboard', 'price.snapshot', renderPriceSnapshot);
        scheduleRender('badges.spot', updateBadges);         // the header spot badge reads priceChart.latest
    }

    function renderPriceSnapshot() {
        if (!priceChart.candles) return;
        const candles = Array.from(priceChart.bars.values()).sort((a, b) => a.time - b.time);
        const ts = priceChart.chart.timeScale();
        const keep = priceChart.needsFit ? null : ts.getVisibleLogicalRange();
        const oldLen = priceChart.candles.data().length;
        priceChart.candles.setData(candles);
        priceChart.rsi.setData(_rsiPoints(candles));
        const pts = Array.from(priceChart.overnightPts.values()).sort((a, b) => a.time - b.time);   // unique times
        priceChart.overnight.setData(pts);
        priceChart.lastTime = candles.length ? candles[candles.length - 1].time : null;
        priceChart.lastOvernightTime = pts.length ? pts[pts.length - 1].time : null;
        priceChart.pendingBars.clear();
        priceChart.pendingOvernight = [];
        renderOverlays();
        renderFlow();
        renderSignalMarkers();
        if (priceChart.needsFit) {
            ts.fitContent();
            priceChart.needsFit = false;
        } else if (keep) {
            // A viewer at the live edge stays at it when the snapshot carries newer bars; a scrolled-back one keeps the range.
            const shift = keep.to >= oldLen - 2 ? Math.max(0, candles.length - oldLen) : 0;
            ts.setVisibleLogicalRange({ from: keep.from + shift, to: keep.to + shift });
        }
    }

    function handlePriceBar(data) {
        if (!data || !data.bar || (data.session_date || null) !== priceChart.sessionDate) return;
        const c = _toCandle(data.bar);
        if (c.time === null) return;
        priceChart.bars.set(c.time, c);
        if (!priceChart.latest || c.time >= priceChart.latest.time) priceChart.latest = c;
        scheduleRender('badges.spot', updateBadges);
        if (state.activeTab !== 'dashboard') {
            renderWhenVisible('dashboard', 'price.snapshot', renderPriceSnapshot);   // one full redraw on show
            return;
        }
        priceChart.pendingBars.set(c.time, c);
        scheduleRender('price.bars', renderPendingBars);
    }

    function renderPendingBars() {
        if (!priceChart.candles) return;
        const pending = Array.from(priceChart.pendingBars.values()).sort((a, b) => a.time - b.time);
        priceChart.pendingBars.clear();
        if (!pending.length) return;
        if (priceChart.lastTime !== null && pending[0].time < priceChart.lastTime) {
            renderPriceSnapshot();          // only the last bar can be updated in place
            return;
        }
        for (const c of pending) {
            priceChart.candles.update(c);
            priceChart.lastTime = c.time;
        }
        // Only the pending bars' RSI can change (earlier values depend on earlier closes only). Recomputing
        // over the session is a few hundred additions; the series gets in-place updates.
        const rsiByTime = new Map(_rsiPoints(Array.from(priceChart.bars.values()).sort((a, b) => a.time - b.time))
            .map(p => [p.time, p]));
        for (const c of pending) {
            const pt = rsiByTime.get(c.time);
            if (pt) priceChart.rsi.update(pt);
        }
        renderOverlays();                                    // the lines and heatmap run on to the new bar
        if (priceChart.signalsOn && priceChart.signals.length) renderSignalMarkers();   // a signal may now snap to this bar
    }

    function handlePriceOvernight(data) {
        const p = data && data.point;
        if (!p) return;
        const t = etIsoToChartTime(p.time);
        if (t === null) return;
        const point = { time: t, value: p.value };
        priceChart.overnightPts.set(t, point);
        if (state.activeTab !== 'dashboard') {
            renderWhenVisible('dashboard', 'price.snapshot', renderPriceSnapshot);
            return;
        }
        priceChart.pendingOvernight.push(point);
        scheduleRender('price.overnight', renderPendingOvernight);
    }

    function renderPendingOvernight() {
        if (!priceChart.overnight) return;
        const pending = priceChart.pendingOvernight;
        priceChart.pendingOvernight = [];
        for (const pt of pending) {
            if (priceChart.lastOvernightTime !== null && pt.time < priceChart.lastOvernightTime) {
                renderPriceSnapshot();      // out of order: rebuild the line from the model
                return;
            }
            priceChart.overnight.update(pt);
            priceChart.lastOvernightTime = pt.time;
        }
        renderOverlays();
    }

    // One history row: the chart minute and the four levels (null = not published), rounded to cents
    // as the server rounds them, so a row built here compares equal to one from the snapshot.
    function _levelRow(p) {
        if (!p) return null;
        const time = etIsoToChartTime(p.time || p.timestamp);
        if (time === null) return null;
        const row = { time };
        for (const lv of PRICE_LEVELS) {
            const v = p[lv.key];
            row[lv.key] = v == null ? null : Number(Number(v).toFixed(2));
        }
        return row;
    }

    // Adds the row when it changes any level (a second change in the same minute replaces the
    // first, as on the server); true when the model changed. An older row is ignored.
    function recordLevelRow(row) {
        if (!row) return false;
        const rows = priceChart.levelRows;
        const last = rows.length ? rows[rows.length - 1] : null;
        if (last && row.time < last.time) return false;
        if (last && PRICE_LEVELS.every(lv => last[lv.key] === row[lv.key])) return false;
        if (last && last.time === row.time) rows[rows.length - 1] = row;
        else rows.push(row);
        if (rows.length > LEVEL_ROWS_MAX) rows.shift();
        return true;
    }

    // Step-line points of one level. ``rows`` are sorted; a row before ``startTime`` (the first bar)
    // is drawn from the first bar. A level that is not published is a gap (a point with no value). The
    // last value runs on to ``endTime``.
    function levelLinePoints(rows, key, startTime, endTime) {
        const byTime = new Map();
        for (const r of rows) {
            const t = startTime !== null && r.time < startTime ? startTime : r.time;
            byTime.set(t, r[key] === null ? { time: t } : { time: t, value: r[key] });
        }
        const pts = [];
        for (const p of byTime.values()) {
            const prev = pts.length ? pts[pts.length - 1] : null;
            if (prev ? prev.value === p.value : p.value === undefined) continue;   // no change, or a gap before any value
            pts.push(p);
        }
        const last = pts.length ? pts[pts.length - 1] : null;
        if (last && last.value !== undefined && endTime !== null && endTime > last.time) {
            pts.push({ time: endTime, value: last.value });
        }
        return pts;
    }

    // The first bar and the last time on the chart's time scale (bars, overnight line and level lines).
    function _chartTimeSpan() {
        const rows = priceChart.levelRows;
        const times = Array.from(priceChart.bars.keys());
        const ends = times.concat(Array.from(priceChart.overnightPts.keys()), rows.length ? [rows[rows.length - 1].time] : []);
        return { start: times.length ? Math.min(...times) : null, end: ends.length ? Math.max(...ends) : null };
    }

    // Redraws the four step lines from the model: a few dozen points each, so a full setData per
    // bar is cheap and keeps the lines identical after a snapshot, a bar and a hidden period.
    function renderLevels() {
        if (!priceChart.candles) return;
        const { start, end } = _chartTimeSpan();
        for (const lv of PRICE_LEVELS) {
            priceChart.levelSeries[lv.key].setData(levelLinePoints(priceChart.levelRows, lv.key, start, end));
        }
    }

    // ---- Charm heatmap: one frame per minute (strikes + dealer charm exposure), drawn behind the candles.
    function _charmFrame(p) {
        if (!p || !Array.isArray(p.strikes) || !Array.isArray(p.values) || p.strikes.length !== p.values.length
            || !p.strikes.length) return null;
        const time = etIsoToChartTime(p.time);
        return time === null ? null : { time, strikes: p.strikes, values: p.values };
    }

    // Adds the frame (a later one in the same minute replaces it; an older one is ignored); true when
    // the model changed.
    function recordCharmFrame(frame) {
        if (!frame) return false;
        const frames = priceChart.charmFrames;
        const last = frames.length ? frames[frames.length - 1] : null;
        if (last && frame.time < last.time) return false;
        if (last && last.time === frame.time) frames[frames.length - 1] = frame;
        else frames.push(frame);
        if (frames.length > CHARM_FRAMES_MAX) frames.shift();
        priceChart.charmVersion++;
        return true;
    }

    function renderCharm() {
        if (!priceChart.charmPrimitive) return;
        priceChart.charmPrimitive.setFrames(priceChart.charmFrames, _chartTimeSpan().end, priceChart.charmVersion);
    }

    function renderOverlays() {          // everything drawn from the level and charm models
        renderLevels();
        renderCharm();
    }

    function _charmColors() {
        const c = themeColors();
        return { positive: c.info, negative: c.accent };       // supportive / suppressive
    }

    function _syncCharmToggle() {
        const btn = document.getElementById('charmToggle');
        if (btn) btn.classList.toggle('active', priceChart.charmOn);
    }

    // The Charm button: shows or hides the heatmap; the choice is remembered in this browser.
    function setCharmHeatmap(on) {
        priceChart.charmOn = !!on;
        if (priceChart.charmPrimitive) priceChart.charmPrimitive.setVisible(priceChart.charmOn);
        try { localStorage.setItem(CHARM_PREF_KEY, priceChart.charmOn ? '1' : '0'); } catch (e) { /* not remembered */ }
        _syncCharmToggle();
    }
    function toggleCharmHeatmap() { setCharmHeatmap(!priceChart.charmOn); }

    // Called with each published GEX (about every 10 s). It records the levels and the charm frame
    // only when they changed.
    function updatePriceLevels(gexData) {
        if (!priceChart.candles || !gexData) return;
        // A new expiration is a new history (the server resets at the same moment).
        let flowChanged = false;
        if (gexData.expiration) {
            if (priceChart.levelExpiration !== null && priceChart.levelExpiration !== gexData.expiration) {
                priceChart.levelRows = [];
                priceChart.charmFrames = [];
                priceChart.charmVersion++;
                if (priceChart.flowMinutes.size) { priceChart.flowMinutes = new Map(); flowChanged = true; }
            }
            priceChart.levelExpiration = gexData.expiration;
        }
        const row = _levelRow({ ...gexData, time: gexData.timestamp ||
            (priceChart.latest ? new Date(priceChart.latest.time * 1000).toISOString() : null) });
        const levelsChanged = recordLevelRow(row);
        const charmChanged = recordCharmFrame(_charmFrame(gexData.charm_frame));
        const minuteChanged = recordFlowMinute(gexData.dealer && gexData.dealer.flow ? gexData.dealer.flow.minute : null);
        flowChanged = flowChanged || minuteChanged;
        if (!levelsChanged && !charmChanged && !flowChanged) return;
        if (state.activeTab !== 'dashboard') {
            renderWhenVisible('dashboard', 'price.snapshot', renderPriceSnapshot);   // one full redraw on show
            return;
        }
        if (levelsChanged) renderLevels();
        renderCharm();
        if (flowChanged) renderFlow();
    }

    // ---- Hedge-flow pane: the dealers' hedge dollars per minute (+ bought, - sold) as a histogram, and the day's
    // running total as a line on its own scale. One minute per time (the last wins); the snapshot's series
    // and the minute each GEX publish carries (dealer.flow.minute) feed it.
    function recordFlowMinute(m) {
        if (!m || typeof m.time !== 'string') return false;
        const t = etIsoToChartTime(m.time);
        if (t === null) return false;
        const hedge = Number(m.hedge_pressure_usd);
        if (!isFinite(hedge)) return false;
        const row = { time: t, hedge, call: Number(m.call_delta_usd) || 0, put: Number(m.put_delta_usd) || 0 };
        const prev = priceChart.flowMinutes.get(t);
        if (prev && prev.hedge === row.hedge && prev.call === row.call && prev.put === row.put) return false;
        priceChart.flowMinutes.set(t, row);
        return true;
    }

    function renderFlow() {
        if (!priceChart.flowSeries) return;
        const c = themeColors();
        const mins = Array.from(priceChart.flowMinutes.values()).sort((a, b) => a.time - b.time);
        const hist = [], total = [];
        let cum = 0;
        for (const m of mins) {
            cum += m.hedge;
            hist.push({ time: m.time, value: m.hedge, color: m.hedge >= 0 ? c.up : c.down });
            total.push({ time: m.time, value: cum });
        }
        priceChart.flowSeries.setData(hist);
        priceChart.flowCum.setData(total);
    }

    // The pane is pane 2 (under RSI) with stretch factor 1: a histogram of the minutes and the day's total on
    // the left scale. The series and the pane are created together and removed together.
    function _addFlowPane() {
        const LC = LightweightCharts;
        const chart = priceChart.chart;
        const { c } = chartTheme();
        const fmt = { type: 'custom', formatter: (v) => fmtGex(v), minMove: 1 };
        priceChart.flowSeries = chart.addSeries(LC.HistogramSeries, {
            priceLineVisible: false, lastValueVisible: true, title: 'Hedge flow', priceFormat: fmt,
        }, 2);
        // The day total has its own scale (left), kept hidden: a visible left scale makes Lightweight Charts 5.2.1
        // throw "Value is null" while painting this chart. Its level is in the Hedge flow 30m summary tooltip.
        priceChart.flowCum = chart.addSeries(LC.LineSeries, {
            color: c.textMuted, lineWidth: 1, priceLineVisible: false, lastValueVisible: false,
            crosshairMarkerVisible: false, title: 'Day', priceScaleId: 'left',
        }, 2);
        chart.panes()[2].setStretchFactor(1);
        renderFlow();
    }

    function _removeFlowPane() {
        const chart = priceChart.chart;
        for (const s of [priceChart.flowSeries, priceChart.flowCum]) {
            if (!s) continue;
            try { chart.removeSeries(s); } catch (e) { /* already gone */ }
        }
        priceChart.flowSeries = null;
        priceChart.flowCum = null;
        if (chart.panes().length > 2) chart.removePane(2);
    }

    // A failing pane must not take the candles or RSI down with it (the caller is the chart's own build): the pane
    // is removed as far as it was built, the Flow button shows it off, and a later Flow click tries again.
    function _tryAddFlowPane() {
        try {
            _addFlowPane();
        } catch (e) {
            console.error('Hedge-flow pane unavailable; the candles and RSI still draw', e);
            try { _removeFlowPane(); } catch (e2) { /* nothing more to clean up */ }
            priceChart.flowSeries = null;
            priceChart.flowCum = null;
            priceChart.flowOn = false;
        }
    }

    function _syncFlowToggle() {
        const btn = document.getElementById('flowToggle');
        if (btn) btn.classList.toggle('active', priceChart.flowOn);
    }

    // The Flow button: shows or hides the hedge-flow pane; the choice is remembered in this browser.
    function setFlowPane(on) {
        priceChart.flowOn = !!on;
        if (priceChart.chart) {
            if (priceChart.flowOn && !priceChart.flowSeries) _tryAddFlowPane();
            else if (!priceChart.flowOn && priceChart.flowSeries) _removeFlowPane();
        }
        // The stored choice is the click, not the outcome: a pane that failed to build is retried on the next load.
        try { localStorage.setItem(FLOW_PANE_KEY, on ? '1' : '0'); } catch (e) { /* not remembered */ }
        _syncFlowToggle();
    }
    function toggleFlowPane() { setFlowPane(!priceChart.flowOn); }

    function applyPriceChartTheme() {
        if (!priceChart.chart) return;
        const { bg, grid, text, c } = chartTheme();
        priceChart.chart.applyOptions({
            layout: { background: { type: 'solid', color: bg }, textColor: text },
            grid: { vertLines: { color: grid }, horzLines: { color: grid } },
            timeScale: { borderColor: grid }, rightPriceScale: { borderColor: grid },
        });
        priceChart.candles.applyOptions({ upColor: c.up, downColor: c.down, wickUpColor: c.up, wickDownColor: c.down });
        priceChart.overnight.applyOptions({ color: c.accent });
        priceChart.rsi.applyOptions({ color: c.alt });
        for (const g of priceChart.rsiGuides) g.applyOptions({ color: c.textFaint });
        for (const lv of PRICE_LEVELS) {
            priceChart.levelSeries[lv.key].applyOptions({ color: levelColor(c, lv) });
        }
        priceChart.charmPrimitive.refresh();
        if (priceChart.flowSeries) {
            priceChart.flowCum.applyOptions({ color: c.textMuted });
            renderFlow();                                    // the bars' up / down colors are per point
        }
        renderSignalMarkers();                               // marker colors are tokens: rebuilt in the new theme
    }
    window.addEventListener('themechange', applyPriceChartTheme);      // cheap: runs immediately, shown or not

    // ---- Signal markers: the server's signal records (signals.py) as markers on the candles. Off by default.
    // A record sits on the latest bar at or before its minute; before the first bar it has no marker.
    // Shape, side and color token by type (spec 3.1): zero gamma and EM are circles, node holds arrows at the pin,
    // node breaks squares, flow surges and vanna shifts arrows in their direction, divergences and charm circles.
    function _signalStyle(s) {
        const up = s.side === 'bull';
        const side = up ? 'belowBar' : 'aboveBar';
        switch (s.type) {
            case 'zg_cross': return { shape: 'circle', color: 'accent', position: side };
            case 'node_hold': return { shape: up ? 'arrowUp' : 'arrowDown', color: up ? 'up' : 'down', position: side };
            case 'node_break': return { shape: 'square', color: 'alt', position: side };
            case 'em_touch': return { shape: 'circle', color: 'info', position: /-$/.test(s.text || '') ? 'belowBar' : 'aboveBar' };
            case 'flow_surge': return { shape: up ? 'arrowUp' : 'arrowDown', color: 'info', position: side };
            case 'flow_div': return { shape: 'circle', color: 'alt', position: side };
            case 'vanna_shift': return { shape: up ? 'arrowUp' : 'arrowDown', color: 'info', position: side };
            case 'charm_window': return { shape: 'circle', color: 'info', position: side };
            default: return null;
        }
    }

    // Markers for the signals; ``barTimes`` are the chart times of the bars, ascending. Pure apart from the theme read.
    function signalMarkerList(signals, barTimes) {
        if (!Array.isArray(signals) || !Array.isArray(barTimes) || !barTimes.length) return [];
        const c = themeColors();
        const out = [];
        for (const s of signals) {
            const t = etIsoToChartTime(s && s.time);
            const style = s ? _signalStyle(s) : null;
            if (t === null || !style) continue;
            let snap = null;                                  // the latest bar at or before the signal's minute
            for (const bt of barTimes) {
                if (bt <= t) snap = bt; else break;
            }
            if (snap === null) continue;
            out.push({ time: snap, position: style.position, color: c[style.color], shape: style.shape, text: s.text || '' });
        }
        return out.sort((a, b) => a.time - b.time);
    }

    function renderSignalMarkers() {
        if (!priceChart.signalMarkers) return;
        const times = Array.from(priceChart.bars.keys()).sort((a, b) => a - b);
        priceChart.signalMarkers.setMarkers(priceChart.signalsOn ? signalMarkerList(priceChart.signals, times) : []);
    }

    // A live signal record (the "signal" WS message). It is kept even while the markers are off.
    function handleSignal(rec) {
        if (!rec || typeof rec.time !== 'string' || !rec.type) return;
        priceChart.signals.push(rec);
        if (priceChart.signals.length > SIGNALS_MAX) priceChart.signals.shift();
        if (!priceChart.signalsOn) return;
        renderWhenVisible('dashboard', 'price.signals', renderSignalMarkers);
    }

    function _syncSignalToggle() {
        const btn = document.getElementById('signalToggle');
        if (btn) btn.classList.toggle('active', priceChart.signalsOn);
    }

    // The Signals button: shows or hides the markers; the choice is remembered in this browser.
    function setSignalMarkers(on) {
        priceChart.signalsOn = !!on;
        try { localStorage.setItem(SIGNAL_MARKERS_KEY, priceChart.signalsOn ? '1' : '0'); } catch (e) { /* not remembered */ }
        _syncSignalToggle();
        renderSignalMarkers();
    }
    function toggleSignalMarkers() { setSignalMarkers(!priceChart.signalsOn); }

    function onDashboardShown() {
        flushHiddenDirty('dashboard');
    }

    // ======================================================================
