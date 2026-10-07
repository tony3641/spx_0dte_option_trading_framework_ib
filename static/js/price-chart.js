    // SPX price chart (TradingView Lightweight Charts). Times are ET wall-clock encoded as UTC
    // seconds, so the axis shows ET whatever the PC's time zone.
    // ======================================================================
    const PRICE_LEVELS = [
        { key: 'call_wall', color: 'up', title: 'Call Wall', dashed: false },
        { key: 'put_wall', color: 'down', title: 'Put Wall', dashed: false },
        { key: 'gamma_flip', color: 'accent', title: 'Gamma Flip', dashed: true },
        { key: 'max_pain', color: 'info', title: 'Max Pain', dashed: true },
    ];
    // The model (bars, overnightPts, latest) is always current; the series are brought up to date
    // only while the Dashboard tab is visible. pending* hold what the next frame must push.
    const priceChart = {
        chart: null, candles: null, overnight: null,
        sessionDate: null, bars: new Map(), overnightPts: new Map(), latest: null,
        pendingBars: new Map(), pendingOvernight: [],
        lastTime: null, lastOvernightTime: null,      // last time written to each series
        levels: {}, levelValues: {}, needsFit: false, hadSnapshot: false,
    };

    function etIsoToChartTime(iso) {
        const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(iso || '');
        if (!m) return null;
        return Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]) / 1000;
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
            priceChart.chart = chart;                       // published together: never a half-built chart
            priceChart.candles = candles;
            priceChart.overnight = overnight;
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
        const pts = Array.from(priceChart.overnightPts.values()).sort((a, b) => a.time - b.time);   // unique times
        priceChart.overnight.setData(pts);
        priceChart.lastTime = candles.length ? candles[candles.length - 1].time : null;
        priceChart.lastOvernightTime = pts.length ? pts[pts.length - 1].time : null;
        priceChart.pendingBars.clear();
        priceChart.pendingOvernight = [];
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
    }

    // Price-line updates are cheap and happen at most every 10 s, so this runs directly.
    function updatePriceLevels(gexData) {
        if (!priceChart.candles || !gexData) return;
        const LC = LightweightCharts;
        for (const lv of PRICE_LEVELS) {
            const val = gexData[lv.key];
            const have = priceChart.levels[lv.key];
            if (val == null) {
                if (have) { priceChart.candles.removePriceLine(have); delete priceChart.levels[lv.key]; }
                delete priceChart.levelValues[lv.key];
                continue;
            }
            if (priceChart.levelValues[lv.key] === val) continue;
            priceChart.levelValues[lv.key] = val;
            const opts = { price: val, color: themeColors()[lv.color], lineWidth: 1, axisLabelVisible: true,
                           lineStyle: lv.dashed ? LC.LineStyle.Dashed : LC.LineStyle.Solid,
                           title: `${lv.title} ${val}` };
            if (have) have.applyOptions(opts);
            else priceChart.levels[lv.key] = priceChart.candles.createPriceLine(opts);
        }
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
        for (const lv of PRICE_LEVELS) {
            const line = priceChart.levels[lv.key];
            if (line) line.applyOptions({ color: c[lv.color] });
        }
    }
    window.addEventListener('themechange', applyPriceChartTheme);      // cheap: runs immediately, shown or not

    function onDashboardShown() {
        flushHiddenDirty('dashboard');
    }

    // ======================================================================
