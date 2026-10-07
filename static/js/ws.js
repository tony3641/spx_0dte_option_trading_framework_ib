    // WebSocket
    // ======================================================================
    let ws = null;
    let reconnectTimer = null;
    const RECONNECT_DELAYS_MS = [500, 1000, 2000, 3000];   // then every 3 s; reset when a socket opens
    let reconnectAttempt = 0;

    function connectWS() {
        const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const url = `${proto}//${location.host}/ws`;
        console.log('Connecting to', url);

        ws = new WebSocket(url);

        ws.onopen = () => {
            console.log('WebSocket connected');
            state.wsConnected = true;
            reconnectAttempt = 0;
            document.getElementById('loadingOverlay').classList.add('hidden');
            ws.send(`set_tab:${getValidTab(state.activeTab)}`);
            if (state.activeTab === 'chain') {
                setTimeout(() => { reportChainViewportCenter(true); }, 120);
            }
            if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
        };

        ws.onclose = () => {
            console.log('WebSocket closed');
            state.wsConnected = false;
            document.getElementById('connDot').className = 'dot dot-red';
            document.getElementById('connStatus').textContent = 'Disconnected';
            // Show overlay with reconnecting message
            document.getElementById('overlayMsg').textContent = 'Reconnecting...';
            document.getElementById('loadingOverlay').classList.remove('hidden');
            scheduleReconnect();
        };

        ws.onerror = (e) => {
            console.error('WebSocket error', e);
        };

        ws.onmessage = (event) => {
            try {
                const msg = JSON.parse(event.data);
                // Diagnostics never drop a data message, even if perf.js failed to load.
                if (typeof perfOnMessage === 'function') perfOnMessage(msg);
                handleMessage(msg);
                if (typeof perfAfterHandle === 'function') perfAfterHandle();
            } catch (e) {
                const snippet = typeof event.data === 'string' ? event.data.slice(0, 240) : '[non-string payload]';
                console.error('Failed to parse message', e, snippet);
            }
        };
    }

    function scheduleReconnect() {
        if (reconnectTimer) return;
        const delay = RECONNECT_DELAYS_MS[Math.min(reconnectAttempt, RECONNECT_DELAYS_MS.length - 1)];
        reconnectAttempt += 1;
        reconnectTimer = setTimeout(() => {
            reconnectTimer = null;
            connectWS();
        }, delay);
    }

    // ======================================================================
