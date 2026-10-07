    // Frame batching: one requestAnimationFrame flush per frame, keyed (latest fn per key wins).
    // Hidden-tab work is parked per tab and scheduled once when the tab is shown.
    // ======================================================================
    const _renderJobs = new Map();
    let _renderFrame = 0;
    const _hiddenDirty = { dashboard: new Map(), chain: new Map(), account: new Map() };

    function scheduleRender(key, fn) {
        _renderJobs.set(key, fn);
        if (!_renderFrame) _renderFrame = requestAnimationFrame(flushRenders);
    }

    function isRenderPending(key) {
        return _renderJobs.has(key);
    }

    // True while any job waits for the next frame flush.
    function hasPendingRenders() {
        return _renderJobs.size > 0;
    }

    function flushRenders() {
        _renderFrame = 0;
        const jobs = Array.from(_renderJobs.values());
        _renderJobs.clear();
        const t0 = performance.now();
        for (const fn of jobs) {
            try { fn(); } catch (e) { console.error('render job failed', e); }
        }
        if (typeof perfMarkFlush === 'function') perfMarkFlush(t0, performance.now());
    }

    // Run `fn` in the next frame when `tab` is the active one; otherwise park it (latest per key)
    // until flushHiddenDirty(tab). A tab without a parking map is never deferred-rendered.
    function renderWhenVisible(tab, key, fn) {
        const parked = _hiddenDirty[tab];
        if (state.activeTab === tab) {
            if (parked) parked.delete(key);          // the fresh job supersedes a parked one
            scheduleRender(key, fn);
        } else if (parked) {
            parked.set(key, fn);
        }
    }

    function flushHiddenDirty(tab) {
        const parked = _hiddenDirty[tab];
        if (!parked) return;
        for (const [key, fn] of parked) scheduleRender(key, fn);
        parked.clear();
    }

    // ======================================================================
