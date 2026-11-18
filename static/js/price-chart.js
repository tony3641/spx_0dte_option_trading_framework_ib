    // SPX price chart (TradingView Lightweight Charts). Times are ET wall-clock encoded as UTC
    // seconds, so the axis shows ET whatever the PC's time zone.
    // ======================================================================
    // Each level is a dotted step line: it starts at the first bar (or when the level first appeared),
    // moves up or down at the minute GEX published a different value, and runs on to the latest bar.
    const PRICE_LEVELS = [
        { key: 'call_wall', color: 'up', name: 'Call Wall', tag: 'CW' },
        { key: 'put_wall', color: 'down', name: 'Put Wall', tag: 'PW' },
        { key: 'gamma_flip', color: 'accent', name: 'Gamma Flip', tag: 'GF' },
        { key: 'max_pain', color: 'info', name: 'Max Pain', tag: 'MP' },
    ];
    // The axis tag of a level is its two-letter code, and the line and tag are translucent so the candles
    // behind them stay readable (the full names are in the "?" help and the Levels strip).
    const LEVEL_ALPHA = 0.7;
    const levelColor = (c, lv) => withAlpha(c[lv.color], LEVEL_ALPHA);
    const LEVEL_ROWS_MAX = 2000;
    const CHARM_FRAMES_MAX = 1440;
    const CHARM_PREF_KEY = 'spx-charm-heatmap';          // localStorage '1' | '0'; default off
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
                    color: levelColor(c, lv), lineWidth: 2, lineStyle: LC.LineStyle.Dotted, lineType: LC.LineType.WithSteps,
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
            chart.panes()[0].setStretchFactor(3);          // RSI gets a quarter of the height (the
            chart.panes()[1].setStretchFactor(1);          // library's own default factors are not 1:1)
            const rsiGuides = [70, 30].map(price => rsi.createPriceLine({
                price, color: c.textFaint, lineWidth: 1, lineStyle: LC.LineStyle.Dashed, axisLabelVisible: false,
            }));
            priceChart.chart = chart;                       // published together: never a half-built chart
            priceChart.candles = candles;
            priceChart.overnight = overnight;
            priceChart.levelSeries = levelSeries;
            priceChart.charmPrimitive = charmPrimitive;
            priceChart.rsi = rsi;
            priceChart.rsiGuides = rsiGuides;
            let saved = null;
            try { saved = localStorage.getItem(CHARM_PREF_KEY); } catch (e) { /* storage blocked: stays off */ }
            setCharmHeatmap(saved === '1');
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
        if (gexData.expiration) {
            if (priceChart.levelExpiration !== null && priceChart.levelExpiration !== gexData.expiration) {
                priceChart.levelRows = [];
                priceChart.charmFrames = [];
                priceChart.charmVersion++;
            }
            priceChart.levelExpiration = gexData.expiration;
        }
        const row = _levelRow({ ...gexData, time: gexData.timestamp ||
            (priceChart.latest ? new Date(priceChart.latest.time * 1000).toISOString() : null) });
        const levelsChanged = recordLevelRow(row);
        const charmChanged = recordCharmFrame(_charmFrame(gexData.charm_frame));
        if (!levelsChanged && !charmChanged) return;
        if (state.activeTab !== 'dashboard') {
            renderWhenVisible('dashboard', 'price.snapshot', renderPriceSnapshot);   // one full redraw on show
            return;
        }
        if (levelsChanged) renderLevels();
        renderCharm();
    }

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
    }
    window.addEventListener('themechange', applyPriceChartTheme);      // cheap: runs immediately, shown or not

    function onDashboardShown() {
        flushHiddenDirty('dashboard');
    }

    // ======================================================================
