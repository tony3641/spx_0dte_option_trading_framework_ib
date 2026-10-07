    // Client perf: server ts -> receipt, receipt -> frame flush, long tasks. Sent every 10 s.
    // Span names must match the server's ^[a-z_]+(\.[a-z_]+)*$ (see web/push.py record_client_perf):
    // message types are lowercase with underscores, so `<type>.recv` / `<type>.paint` qualify.
    // ======================================================================
    const PERF_REPORT_MS = 10000;
    const PERF_MAX_SAMPLES = 200;
    // Message types whose handling ends in a paint, and the tab that paints them (null = always
    // visible). A message for a hidden tab is parked, not painted, so it records no paint span.
    const PERF_PAINT_TABS = {
        chain_tick: 'chain', chain_quotes: 'chain', account_update: 'account',
        price_bar: 'dashboard', price_snapshot: 'dashboard', gex: 'dashboard', status: null,
    };
    const _perfSpans = {};
    let _perfPending = [];

    function _perfPush(name, ms) {
        const arr = _perfSpans[name] || (_perfSpans[name] = []);
        arr.push(+ms.toFixed(2));
        if (arr.length > PERF_MAX_SAMPLES) arr.shift();
    }

    function perfOnMessage(msg) {
        if (!msg || !msg.type) return;
        if (typeof msg.ts === 'number') {
            const d = Date.now() - msg.ts;
            if (d >= 0 && d < 60000) _perfPush(`${msg.type}.recv`, d);
        }
        const tab = PERF_PAINT_TABS[msg.type];
        if (tab !== undefined && (tab === null || tab === state.activeTab)) {
            _perfPending.push([msg.type, performance.now()]);
            if (_perfPending.length > 500) _perfPending.shift();
        }
    }

    function perfMarkFlush(t0, t1) {
        for (const [type, tRecv] of _perfPending) _perfPush(`${type}.paint`, t1 - tRecv);
        _perfPending = [];
    }

    try {
        new PerformanceObserver(list => {
            for (const e of list.getEntries()) _perfPush('longtask', e.duration);
        }).observe({ type: 'longtask', buffered: true });
    } catch (e) { /* long tasks not supported */ }

    function perfSendReport() {
        if (!ws || ws.readyState !== WebSocket.OPEN) return;
        const names = Object.keys(_perfSpans);
        if (!names.length) return;
        const spans = {};
        for (const n of names) { spans[n] = _perfSpans[n]; delete _perfSpans[n]; }
        ws.send('perf_report:' + JSON.stringify({ spans }));
    }

    setInterval(perfSendReport, PERF_REPORT_MS);

    // ======================================================================
