// Synthetic feed for the render benchmark. Invented data only (2099-01-02, strikes 5850-6445).
// protocol 'v1' = the message mix before sub-project 2; 'v2' = the new mix.
(function () {
    function nextFrames() {
        return new Promise(res => requestAnimationFrame(() => requestAnimationFrame(res)));
    }
    window.__benchInject = async function (msgs) {
        const list = Array.isArray(msgs) ? msgs : [msgs];
        const t0 = performance.now();
        for (const m of list) handleMessage(m);
        await nextFrames();
        return performance.now() - t0;
    };

    let seed = 12345;
    const rnd = () => (seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648;
    const SESSION = '2099-01-02';
    const SPOT = 6150;
    const STRIKES = Array.from({ length: 120 }, (_, i) => 5850 + 5 * i);
    const STREAMED = STRIKES.filter(s => Math.abs(s - SPOT) <= 150);     // about 60 strikes
    const quotes = {};
    for (const s of STRIKES) {
        for (const r of ['C', 'P']) {
            const itm = r === 'C' ? Math.max(0, SPOT - s) : Math.max(0, s - SPOT);
            const mid = Math.max(0.05, itm + 8 * Math.exp(-Math.abs(s - SPOT) / 60));
            quotes[`${s}|${r}`] = {
                bid: +(mid - 0.05).toFixed(2), ask: +(mid + 0.05).toFixed(2),
                bid_size: 10, ask_size: 12, last: +mid.toFixed(2), volume: 100,
                delta: r === 'C' ? 0.5 : -0.5, gamma: 0.002, iv: 15.5,
            };
        }
    }
    function row(s) {
        const c = quotes[`${s}|C`], p = quotes[`${s}|P`];
        const out = { strike: s, sigma_distance_abs: Math.abs(s - SPOT) / 25, sigma_distance_signed: (s - SPOT) / 25 };
        for (const [side, q] of [['call', c], ['put', p]]) {
            for (const f of ['bid', 'ask', 'bid_size', 'ask_size', 'last', 'delta', 'gamma', 'iv']) out[`${side}_${f}`] = q[f];
            out[`${side}_oi`] = 500; out[`${side}_volume`] = q.volume; out[`${side}_age_s`] = 1.0;
        }
        return out;
    }
    function chainPayload(scope, strikes) {
        return {
            strikes: strikes.map(row), spot_price: SPOT, annual_vol: 0.18, expiration_raw: '20990102',
            trading_class: 'SPXW', tte_years: 0.001, sigma_move: 25, call_wall: 6200, put_wall: 6100,
            gamma_flip: 6140, max_age_s: 180, timestamp_iso: new Date().toISOString(), scope,
        };
    }
    function gexPayload() {
        const bars = STRIKES.map(s => ({ strike: s, call_gex: 1e6 * rnd(), put_gex: -1e6 * rnd(), net_gex: 1e5 * (rnd() - 0.5),
                                         call_oi: 500, put_oi: 600, call_vol: 100, put_vol: 120 }));
        const smile = STRIKES.map(s => ({ strike: s, call_iv: 15 + rnd(), put_iv: 16 + rnd(), call_efficiency: rnd(),
                                          put_efficiency: rnd(), call_delta: 0.5, put_delta: -0.5, call_charm: 0.001, put_charm: -0.001 }));
        return { gex_bars: bars, call_wall: 6200, put_wall: 6100, gamma_flip: 6140, max_pain: 6150, spot_price: SPOT,
                 expiration: '20990102', timestamp: '', total_call_gex: 1e8, total_put_gex: -9e7, total_net_gex: 1e7,
                 total_call_oi: 60000, total_put_oi: 72000, total_call_vol: 12000, total_put_vol: 14000, smile_data: smile, es_derived: false };
    }
    function etIso(minuteIndex) {
        const h = 9 + Math.floor((30 + minuteIndex) / 60), m = (30 + minuteIndex) % 60;
        return `${SESSION}T${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:00-05:00`;
    }
    let lastClose = SPOT;
    function bar(i) {
        const o = lastClose, c = +(o + (rnd() - 0.5) * 2).toFixed(2);
        lastClose = c;
        return { time: etIso(i), time_short: etIso(i).slice(11, 16), open: o, high: Math.max(o, c) + 0.5, low: Math.min(o, c) - 0.5, close: c };
    }
    function initMessage(protocol, bars) {
        const data = { connected: true, market_status: 'RTH', expiration: '2099-01-02', spot_price: SPOT, gex: gexPayload(),
                       last_chain_update: '10:00:00', data_mode: 'live', historical_date: SESSION, es_derived: false, es_price: null,
                       chain_quotes: chainPayload('full', STRIKES), account: null, gex_mode: '0dte', monthly_gex: null, monthly_expiration: 'N/A' };
        if (protocol === 'v1') data.price_history = bars;
        else data.price = { session_date: SESSION, mode: 'live', bars, overnight: [] };
        return { type: 'init', data };
    }
    function streamCycle(protocol) {
        const changed = [];
        for (let k = 0; k < 30; k++) {
            const s = STREAMED[Math.floor(rnd() * STREAMED.length)], r = rnd() < 0.5 ? 'C' : 'P';
            const q = quotes[`${s}|${r}`];
            const d = rnd() < 0.5 ? -0.05 : 0.05;
            q.bid = +Math.max(0.05, q.bid + d).toFixed(2); q.ask = +(q.bid + 0.1).toFixed(2); q.volume += 1;
            changed.push({ strike: s, right: r, bid: q.bid, ask: q.ask, volume: q.volume });
        }
        const ts = new Date().toISOString();
        if (protocol === 'v1') {
            const ticks = [];
            for (const s of STREAMED) for (const r of ['C', 'P']) ticks.push({ strike: s, right: r, ...quotes[`${s}|${r}`] });
            return [{ type: 'chain_tick', data: { ticks, timestamp_iso: ts } },
                    { type: 'chain_quotes', data: chainPayload('stream', STREAMED) }];
        }
        return [{ type: 'chain_tick', data: { ticks: changed, timestamp_iso: ts } }];
    }

    window.__renderBench = async function (opts) {
        const protocol = opts.protocol, seconds = opts.seconds, tab = opts.tab;
        const bars = Array.from({ length: 200 }, (_, i) => bar(i));
        let minute = 200, current = bar(minute);
        await window.__benchInject(initMessage(protocol, bars));
        switchTab(tab);
        await new Promise(r => setTimeout(r, 2000));                   // warm-up, not measured

        const metrics = { stream_cycle: [], price_bar: [], full_publish: [] };
        const longtasks = [];
        let obs = null;
        try {
            obs = new PerformanceObserver(list => list.getEntries().forEach(e => longtasks.push(e.duration)));
            obs.observe({ type: 'longtask' });
        } catch (e) { /* not supported: reported as 0 */ }

        const timers = [];
        timers.push(setInterval(async () => metrics.stream_cycle.push(await window.__benchInject(streamCycle(protocol))), 500));
        let ticksInMinute = 0;
        timers.push(setInterval(async () => {
            ticksInMinute += 1;
            let msgs;
            if (ticksInMinute >= 60) {                                 // a new minute
                ticksInMinute = 0; minute += 1;
                const done = current; current = bar(minute);
                msgs = protocol === 'v1' ? [{ type: 'bar', data: done }]
                                         : [{ type: 'price_bar', data: { session_date: SESSION, bar: current } }];
            } else {
                current = { ...current, close: +(current.close + (rnd() - 0.5)).toFixed(2) };
                current.high = Math.max(current.high, current.close); current.low = Math.min(current.low, current.close);
                msgs = protocol === 'v1' ? [{ type: 'bar_update', data: current }]
                                         : [{ type: 'price_bar', data: { session_date: SESSION, bar: current } }];
            }
            metrics.price_bar.push(await window.__benchInject(msgs));
        }, 1000));
        timers.push(setInterval(async () => {
            metrics.full_publish.push(await window.__benchInject([
                { type: 'gex', data: gexPayload() }, { type: 'chain_quotes', data: chainPayload('full', STRIKES) }]));
        }, 10000));

        await new Promise(r => setTimeout(r, seconds * 1000));
        timers.forEach(clearInterval);
        await new Promise(r => setTimeout(r, 500));
        if (obs) obs.disconnect();

        const summarize = arr => {
            if (!arr.length) return { n: 0, p50: null, p95: null, max: null };
            const v = [...arr].sort((a, b) => a - b);
            const pct = p => v[Math.max(0, Math.min(v.length - 1, Math.ceil(p / 100 * v.length) - 1))];
            return { n: v.length, p50: +pct(50).toFixed(1), p95: +pct(95).toFixed(1), max: +v[v.length - 1].toFixed(1) };
        };
        const out = { metrics: {}, longtasks: { count_over_50ms: longtasks.filter(d => d > 50).length,
                                                max_ms: longtasks.length ? +Math.max(...longtasks).toFixed(1) : 0 } };
        for (const k of Object.keys(metrics)) out.metrics[k] = summarize(metrics[k]);
        return out;
    };
})();
