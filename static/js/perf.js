    // Client perf: server ts -> receipt, receipt -> frame flush, long tasks. Sent every 10 s.
    // Span names must fully match the server's [a-z_]+(\.[a-z_]+)* (see web/push.py record_client_perf):
    // message types are lowercase with underscores, so `<type>.recv` / `<type>.paint` qualify.
    // ======================================================================
    const PERF_REPORT_MS = 10000;
    const PERF_MAX_SAMPLES = 200;
    const PERF_MAX_JOB_SAMPLES = 200;         // per job key: a hidden tab never runs its rAF jobs, so cap the wait list
    // `<type>.paint` is recorded only for a message whose handling scheduled render jobs (a message for
    // a hidden tab is parked, a no-op status schedules nothing): it spans receipt to the end of the last
    // job the message scheduled, light or heavy.
    const _perfSpans = {};
    let _perfMsg = null;                      // the message being handled; jobs scheduled now belong to it
    const _perfJobSamples = new Map();        // job key -> paint samples waiting for that job to run

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
        _perfMsg = { type: msg.type, t: performance.now(), jobs: 0 };
    }

    // render-loop.js calls this for every job scheduled; it attaches the message being handled, if any.
    function perfNoteJob(key) {
        if (!_perfMsg) return;
        _perfMsg.jobs += 1;
        const waiting = _perfJobSamples.get(key);
        if (waiting) {
            waiting.push(_perfMsg);
            if (waiting.length > PERF_MAX_JOB_SAMPLES) waiting.shift();     // the oldest message's paint is not recorded
        } else {
            _perfJobSamples.set(key, [_perfMsg]);
        }
    }

    // Call when handleMessage returns (also when it threw): later jobs belong to no message.
    function perfAfterHandle() {
        _perfMsg = null;
    }

    // The jobs under `keys` have run (the paint of everything they drew starts here).
    function perfJobsDone(keys, tEnd) {
        for (const key of keys) {
            const waiting = _perfJobSamples.get(key);
            if (!waiting) continue;
            _perfJobSamples.delete(key);
            for (const s of waiting) {
                s.jobs -= 1;
                if (s.jobs === 0) _perfPush(`${s.type}.paint`, tEnd - s.t);
            }
        }
    }

    // A parked job paints nothing now: its samples are dropped, not recorded.
    function perfJobDropped(key) {
        _perfJobSamples.delete(key);
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
