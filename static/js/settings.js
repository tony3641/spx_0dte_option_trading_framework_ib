    // Settings modal (Discord + IB). Values persist to the server-side .env;
    // applies are hot-applied per section (Discord bot restart / IB reconnect).
    // ======================================================================
    let settingsLoaded = false;

    async function openSettings() {
        document.getElementById('settingsModalBackdrop').classList.remove('hidden');
        if (!settingsLoaded) await loadSettings();
    }

    function closeSettings() {
        document.getElementById('settingsModalBackdrop').classList.add('hidden');
    }

    async function loadSettings() {
        const getJson = url => fetch(url).then(r => {
            if (!r.ok) throw new Error(r.statusText || ('HTTP ' + r.status));
            return r.json();
        });
        // Each section loads on its own: one failing request (a 403 from a non-localhost origin, an older
        // server) must not blank the other sections. A section that failed keeps its Apply disabled, so an
        // Apply never writes the blank fields of a form that did not load.
        const [discordRes, ibRes, simRes, dealerRes] = await Promise.allSettled([
            getJson('/api/settings/discord'),
            getJson('/api/settings/ib'),
            getJson('/api/settings/sim'),
            getJson('/api/settings/dealer'),
        ]);
        const failed = res => 'Failed to load settings: ' + ((res.reason && res.reason.message) || res.reason);
        const applyBtn = id => document.getElementById(id);

        if (discordRes.status === 'fulfilled') {
            const discord = discordRes.value;
            const tok = document.getElementById('setDiscordToken');
            tok.value = '';
            tok.placeholder = discord.token_set
                ? ('•••• ' + (discord.token_hint || ''))
                : 'Not set';
            document.getElementById('setDiscordGuildId').value = discord.guild_id || '';
            document.getElementById('setDiscordChannelId').value = discord.channel_id || '';
            document.getElementById('setDiscordUserIds').value = (discord.allowed_user_ids || []).join(',');
            document.getElementById('setDiscordRole').value = discord.allowed_role || '';
            document.getElementById('setDiscordClearToken').checked = false;
            const status = document.getElementById('setDiscordStatus');
            status.textContent = discord.running ? '● Running'
                : (discord.token_set ? '○ Stopped' : '○ Disabled (no token)');
            status.className = 'settings-status ' + (discord.running ? 'ok' : 'off');
            document.getElementById('setDiscordResult').textContent = '';
            applyBtn('setDiscordApplyBtn').disabled = false;
        } else {
            setSettingsResult('setDiscordResult', failed(discordRes), false);
            applyBtn('setDiscordApplyBtn').disabled = true;
        }

        if (ibRes.status === 'fulfilled') {
            document.getElementById('setIbPort').value = ibRes.value.port;
            document.getElementById('setIbResult').textContent = '';
            applyBtn('setIbApplyBtn').disabled = false;
        } else {
            setSettingsResult('setIbResult', failed(ibRes), false);
            applyBtn('setIbApplyBtn').disabled = true;
        }

        if (simRes.status === 'fulfilled') {
            const sim = simRes.value;
            document.getElementById('setSimWorkers').value = sim.workers;
            document.getElementById('setSimWorkersHint').textContent =
                sim.workers > 0
                    ? `Fixed at ${sim.workers} process${sim.workers === 1 ? '' : 'es'}`
                    : `0 = auto (all ${sim.cpu_count} CPU cores; small runs stay serial)`;
            document.getElementById('setSimResult').textContent = '';
            applyBtn('setSimApplyBtn').disabled = false;
        } else {
            setSettingsResult('setSimResult', failed(simRes), false);
            applyBtn('setSimApplyBtn').disabled = true;
        }

        if (dealerRes.status === 'fulfilled') {
            const dealer = dealerRes.value;
            const modelSel = document.getElementById('setDealerModel');
            modelSel.textContent = '';
            for (const m of (dealer.models || [])) {
                const opt = document.createElement('option');
                opt.value = m;
                opt.textContent = m;
                modelSel.appendChild(opt);
            }
            modelSel.value = dealer.model;
            document.getElementById('setDealerHint').textContent = dealer.model === 'flow_blend'
                ? "Open interest less today's customer flow (needs a few days of flow data to be trusted)"
                : 'Open interest only (dealers long calls, short puts); the default';
            document.getElementById('setDealerResult').textContent = '';
            applyBtn('setDealerApplyBtn').disabled = false;
        } else {
            setSettingsResult('setDealerResult', failed(dealerRes), false);
            applyBtn('setDealerApplyBtn').disabled = true;
        }

        // Only a fully loaded modal is cached: a partial load is retried on the next open.
        settingsLoaded = [discordRes, ibRes, simRes, dealerRes].every(res => res.status === 'fulfilled');
    }

    function setSettingsResult(id, text, ok) {
        const el = document.getElementById(id);
        el.textContent = text;
        el.className = 'settings-result ' + (ok ? 'ok' : 'err');
    }

    async function applyDiscordSettings() {
        const body = {
            guild_id: document.getElementById('setDiscordGuildId').value.trim(),
            channel_id: document.getElementById('setDiscordChannelId').value.trim(),
            allowed_user_ids: document.getElementById('setDiscordUserIds').value.trim(),
            allowed_role: document.getElementById('setDiscordRole').value.trim(),
        };
        if (document.getElementById('setDiscordClearToken').checked) {
            body.token = '';                     // clear = disable
        } else {
            const tok = document.getElementById('setDiscordToken').value.trim();
            if (tok) body.token = tok;           // omitted = keep existing
        }
        const btn = document.getElementById('setDiscordApplyBtn');
        btn.disabled = true;
        btn.textContent = 'Applying...';
        try {
            const r = await fetch('/api/settings/discord', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            const data = await r.json().catch(() => ({}));
            if (!r.ok) throw new Error(data.detail || r.statusText || 'Apply failed');
            let msg = data.running ? 'Applied — bot running.' : 'Applied — Discord disabled.';
            if (data.persisted === false) msg += ' Warning: could not write .env — settings lost on restart.';
            setSettingsResult('setDiscordResult', msg, true);
            settingsLoaded = false;              // reload status on next open
        } catch (e) {
            setSettingsResult('setDiscordResult', 'Apply failed: ' + e.message, false);
        } finally {
            btn.disabled = false;
            btn.textContent = 'Apply Discord';
        }
    }

    async function applyIbSettings() {
        const port = parseInt(document.getElementById('setIbPort').value, 10);
        if (!Number.isInteger(port) || port <= 0 || port > 65535) {
            setSettingsResult('setIbResult', 'Port must be an integer between 1 and 65535.', false);
            return;
        }
        const btn = document.getElementById('setIbApplyBtn');
        btn.disabled = true;
        document.getElementById('overlayMsg').textContent = `Reconnecting IB on port ${port}...`;
        document.getElementById('loadingOverlay').classList.remove('hidden');
        try {
            const r = await fetch('/api/settings/ib', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ port }),
            });
            const data = await r.json().catch(() => ({}));
            if (!r.ok) throw new Error(data.detail || r.statusText || 'Reconnect failed');
            let msg = `Applied — IB reconnected on port ${data.port}.`;
            if (data.persisted === false) msg += ' Warning: could not write .env — port reverts on restart.';
            setSettingsResult('setIbResult', msg, true);
        } catch (e) {
            setSettingsResult('setIbResult',
                'Apply failed: ' + e.message + ' (previous port still saved — retry recovers)', false);
        } finally {
            document.getElementById('loadingOverlay').classList.add('hidden');
            btn.disabled = false;
        }
    }

    async function applyDealerSettings() {
        const model = document.getElementById('setDealerModel').value;
        const btn = document.getElementById('setDealerApplyBtn');
        btn.disabled = true;
        btn.textContent = 'Applying...';
        try {
            const r = await fetch('/api/settings/dealer', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ model }),
            });
            const data = await r.json().catch(() => ({}));
            if (!r.ok) throw new Error(data.detail || r.statusText || 'Apply failed');
            let msg = `Applied — dealer model ${data.model} on the next chain publish.`;
            if (data.persisted === false) msg += ' Warning: could not write .env — setting lost on restart.';
            setSettingsResult('setDealerResult', msg, true);
            settingsLoaded = false;              // reload the hint on next open
        } catch (e) {
            setSettingsResult('setDealerResult', 'Apply failed: ' + e.message, false);
        } finally {
            btn.disabled = false;
            btn.textContent = 'Apply Model';
        }
    }

    async function applySimSettings() {
        const workers = parseInt(document.getElementById('setSimWorkers').value, 10);
        if (!Number.isInteger(workers) || workers < 0) {
            setSettingsResult('setSimResult',
                'Worker processes must be an integer >= 0 (0 = auto).', false);
            return;
        }
        const btn = document.getElementById('setSimApplyBtn');
        btn.disabled = true;
        btn.textContent = 'Applying...';
        try {
            const r = await fetch('/api/settings/sim', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ workers }),
            });
            const data = await r.json().catch(() => ({}));
            if (!r.ok) throw new Error(data.detail || r.statusText || 'Apply failed');
            let msg = workers > 0
                ? `Applied — next sim run uses ${workers} process${workers === 1 ? '' : 'es'}.`
                : `Applied — next sim run uses up to ${data.cpu_count} processes.`;
            if (data.persisted === false) msg += ' Warning: could not write .env — setting lost on restart.';
            setSettingsResult('setSimResult', msg, true);
            settingsLoaded = false;              // reload the hint on next open
        } catch (e) {
            setSettingsResult('setSimResult', 'Apply failed: ' + e.message, false);
        } finally {
            btn.disabled = false;
            btn.textContent = 'Apply Workers';
        }
    }
