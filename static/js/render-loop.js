    // Frame batching. Light jobs: one requestAnimationFrame flush per frame, keyed (latest fn per key wins).
    // Heavy jobs (a Plotly draw takes 15-60 ms) are keyed the same way but run at most ONE per frame,
    // after the light jobs and never in a frame that ran light jobs, so a price bar or chain tick that
    // arrives together with a GEX publish is painted before any chart draw starts and two chart draws
    // never share a frame. Hidden-tab work is parked per tab and scheduled once when the tab is shown.
    // ======================================================================
    const _renderJobs = new Map();       // light jobs: key -> fn
    const _heavyJobs = new Map();        // heavy jobs: key -> {fn, tab, since}
    let _renderFrame = 0;
    let _heavyHop = 0;
    let _flushSeq = 0;
    const HEAVY_MAX_WAIT_FLUSHES = 4;    // a heavy job waits behind busy frames at most this many flushes
    const _hiddenDirty = { dashboard: new Map(), chain: new Map(), account: new Map() };

    // opts.heavy puts the job in the heavy lane; opts.tab (heavy only) makes the lane re-check that the
    // tab is still showing when the job's turn comes and park it otherwise.
    function scheduleRender(key, fn, opts) {
        if (opts && opts.heavy) {
            const queued = _heavyJobs.get(key);
            _heavyJobs.set(key, { fn, tab: opts.tab || null, since: queued ? queued.since : _flushSeq });
            armHeavyLane();
        } else {
            _renderJobs.set(key, fn);
            if (!_renderFrame) _renderFrame = requestAnimationFrame(flushRenders);
        }
        if (typeof perfNoteJob === 'function') perfNoteJob(key);
    }

    function isRenderPending(key) {
        return _renderJobs.has(key) || _heavyJobs.has(key);
    }

    // The lane's frame is requested from a timer, not from inside a flush: rAF callbacks already
    // registered for the next frame, and a flush that a message queues in the meantime, run first.
    function armHeavyLane() {
        if (_renderFrame || _heavyHop) return;
        _heavyHop = setTimeout(() => {
            _heavyHop = 0;
            if (!_renderFrame) _renderFrame = requestAnimationFrame(flushRenders);
        }, 0);
    }

    function flushRenders() {
        _renderFrame = 0;
        const seq = ++_flushSeq;
        const light = Array.from(_renderJobs);
        _renderJobs.clear();
        for (const [, fn] of light) {
            try { fn(); } catch (e) { console.error('render job failed', e); }
        }
        if (light.length && typeof perfJobsDone === 'function') {
            perfJobsDone(light.map(j => j[0]), performance.now());
        }
        if (_heavyJobs.size) {
            const starving = Array.from(_heavyJobs.values()).some(j => seq - j.since >= HEAVY_MAX_WAIT_FLUSHES);
            if (!light.length || starving) runOneHeavyJob();
            if (_heavyJobs.size) armHeavyLane();
        }
    }

    function runOneHeavyJob() {
        for (const [key, job] of Array.from(_heavyJobs)) {
            _heavyJobs.delete(key);
            if (job.tab && state.activeTab !== job.tab) {          // the user left the tab since it was queued
                const parked = _hiddenDirty[job.tab];
                if (parked && !parked.has(key)) parked.set(key, { fn: job.fn, opts: { heavy: true, tab: job.tab } });
                if (typeof perfJobDropped === 'function') perfJobDropped(key);
                continue;                                          // a parked job costs nothing: try the next one
            }
            try { job.fn(); } catch (e) { console.error('render job failed', e); }
            if (typeof perfJobsDone === 'function') perfJobsDone([key], performance.now());
            return;                                                // at most one heavy job per frame
        }
    }

    // Run `fn` in the next frame when `tab` is the active one; otherwise park it (latest per key)
    // until flushHiddenDirty(tab). A tab without a parking map is never deferred-rendered.
    // opts.heavy sends it through the heavy lane (the tab is re-checked when its turn comes).
    function renderWhenVisible(tab, key, fn, opts) {
        const parked = _hiddenDirty[tab];
        const jobOpts = opts && opts.heavy ? { heavy: true, tab } : undefined;
        if (state.activeTab === tab) {
            if (parked) parked.delete(key);          // the fresh job supersedes a parked one
            scheduleRender(key, fn, jobOpts);
        } else if (parked) {
            parked.set(key, { fn, opts: jobOpts });
        }
    }

    function flushHiddenDirty(tab) {
        const parked = _hiddenDirty[tab];
        if (!parked) return;
        for (const [key, job] of parked) scheduleRender(key, job.fn, job.opts);
        parked.clear();
    }

    // ======================================================================
