    // Option Chain Table
    // ======================================================================
    // Cells: one <td> per (strike, side, field), built once and patched in place. A message only
    // updates the model and marks cells dirty; the frame flush (render-loop.js) writes the text.
    // Column order mirrors the header: the call side runs right-to-left, the put side left-to-right.
    const CALL_FIELDS = ['iv', 'volume', 'oi', 'gamma', 'delta', 'ask_size', 'ask', 'bid', 'bid_size'];
    const PUT_FIELDS = ['bid_size', 'bid', 'ask', 'ask_size', 'delta', 'gamma', 'oi', 'volume', 'iv'];
    // Fields a chain_tick may carry. `last` is kept in the model for the order ticket but has no column.
    const TICK_FIELDS = ['bid', 'ask', 'bid_size', 'ask_size', 'volume', 'last', 'delta', 'gamma', 'iv'];
    // What a leg row and the combo summary read: only a change here recomputes the order ticket.
    const LEG_QUOTE_FIELDS = new Set(['bid', 'ask', 'delta']);
    const SIDE_ACTION = { call_ask: ['C', 'BUY'], call_bid: ['C', 'SELL'], put_bid: ['P', 'SELL'], put_ask: ['P', 'BUY'] };
    const CHAIN_SIDES = [['call', 'C', CALL_FIELDS], ['put', 'P', PUT_FIELDS]];
    const chainView = {
        rows: new Map(),         // strike -> {tr, cells: {call_bid: td, ..., strike: td}, strikeHtml}
        dirty: new Map(),        // `${strike}|${side}_${field}` -> value before the first change this frame
        staleState: new Map(),   // `${strike}|${right}` -> whether the side is currently dimmed
        built: false,
    };

    function _legOnStrike(strike, right) {
        return state.strategyLegs.some(l => l.strike === strike && l.right === right);
    }

    function handleChainQuotes(data) {
        if (!data || !data.strikes) return;
        if (data.strikes.length === 0 && Object.keys(state.chainData).length > 0) {
            console.warn('Ignoring empty full chain_quotes payload; preserving existing chain table data');
            return;
        }

        state.chainMeta = {
            spot_price: data.spot_price,
            annual_vol: data.annual_vol,
            expiration_raw: data.expiration_raw,
            trading_class: data.trading_class || 'SPXW',
            tte_years: data.tte_years,
            sigma_move: data.sigma_move,
            call_wall: data.call_wall,
            put_wall: data.put_wall,
            gamma_flip: data.gamma_flip,
            max_age_s: data.max_age_s,
        };
        state.chainLastUpdateMs = data.timestamp_iso ? Date.parse(data.timestamp_iso) : Date.now();
        updateChainUpdateAge();

        // A full payload replaces every row and re-seeds each side's receipt time from its age.
        const now = Date.now();
        state.chainData = {};
        for (const row of data.strikes) {
            state.chainData[row.strike] = row;
            for (const [side, right] of CHAIN_SIDES) {
                const age = row[`${side}_age_s`];
                if (age !== undefined && age !== null) state.chainSideSeenMs[`${row.strike}|${right}`] = now - age * 1000;
            }
        }

        renderWhenVisible('chain', 'chain.structure', renderChainStructure);
        if (state.strategyLegs.length) renderWhenVisible('chain', 'strategy.prices', updateStrategyPrices);
    }

    // A tick carries only the fields that changed (plus strike and right). Unknown strikes are ignored.
    function handleChainTick(data) {
        if (!data || !data.ticks) return;
        state.chainLastUpdateMs = data.timestamp_iso ? Date.parse(data.timestamp_iso) : Date.now();
        const now = Date.now();
        let legTouched = false;
        for (const t of data.ticks) {
            const row = state.chainData[t.strike];
            const side = t.right === 'C' ? 'call' : (t.right === 'P' ? 'put' : null);
            if (!row || !side) continue;
            state.chainSideSeenMs[`${t.strike}|${t.right}`] = now;
            const cells = (chainView.rows.get(t.strike) || {}).cells;
            for (const f of TICK_FIELDS) {
                if (!(f in t)) continue;
                const key = `${side}_${f}`;
                const old = row[key];
                if (old === t[f]) continue;
                row[key] = t[f];
                const ck = `${t.strike}|${key}`;
                if (cells && cells[key] && !chainView.dirty.has(ck)) chainView.dirty.set(ck, old);
                if (LEG_QUOTE_FIELDS.has(f) && _legOnStrike(t.strike, t.right)) legTouched = true;
            }
        }
        renderWhenVisible('chain', 'chain.cells', flushChainCells);
        if (legTouched) renderWhenVisible('chain', 'strategy.prices', updateStrategyPrices);
    }

    // Write the dirty cells from the model. A null value is a real change and shows the empty text.
    function flushChainCells() {
        for (const [ck, oldVal] of chainView.dirty) {
            const bar = ck.indexOf('|');
            const strike = Number(ck.slice(0, bar));
            const key = ck.slice(bar + 1);
            const entry = chainView.rows.get(strike);
            const td = entry && entry.cells[key];
            if (!td) continue;
            const row = state.chainData[strike];
            const val = row ? row[key] : null;
            const txt = formatChainVal(key.slice(key.indexOf('_') + 1), val);
            if (td.textContent !== txt) td.textContent = txt;
            if (typeof val === 'number' && typeof oldVal === 'number' && val !== oldVal && td.animate) {
                td.animate([{ backgroundColor: val > oldVal ? 'rgba(22,163,74,0.38)' : 'rgba(220,38,38,0.38)' },
                            { backgroundColor: 'transparent' }], { duration: 600, easing: 'ease-out' });
            }
        }
        chainView.dirty.clear();
    }

    // One shared formatter: val.toLocaleString() builds a formatter per call and costs about 25x more.
    const CHAIN_COUNT_FORMAT = new Intl.NumberFormat();

    function formatChainVal(field, val) {
        if (val === null || val === undefined) return '-';
        if (field === 'bid' || field === 'ask' || field === 'last') return val.toFixed(2);
        if (field === 'delta') return val.toFixed(3);
        if (field === 'gamma') return val.toFixed(4);
        if (field === 'iv') return val.toFixed(1) + '%';
        if (field === 'bid_size' || field === 'ask_size' || field === 'volume' || field === 'oi')
            return CHAIN_COUNT_FORMAT.format(val);
        return String(val);
    }

    function updateChainUpdateAge() {
        const el = document.getElementById('chainLastUpdateInfo');
        if (!el) return;
        if (!state.chainLastUpdateMs) {
            el.textContent = 'Last update: -';
            return;
        }
        const sec = Math.max(0, Math.floor((Date.now() - state.chainLastUpdateMs) / 1000));
        el.textContent = `Last update: +${sec}s`;
    }

    function hasSelectedLeg(strike, right, action) {
        return state.strategyLegs.some(l => l.strike === strike && l.right === right && l.action === action);
    }

    function refreshSelectionHighlights() {
        document.querySelectorAll('.cell-selected-bid').forEach(el => el.classList.remove('cell-selected-bid'));
        document.querySelectorAll('.cell-selected-ask').forEach(el => el.classList.remove('cell-selected-ask'));
        for (const leg of state.strategyLegs) {
            const side = leg.right === 'C' ? 'call' : 'put';
            const actionSide = leg.action === 'BUY' ? 'ask' : 'bid';
            const cell = document.getElementById(`chain_${leg.strike}_${side}_${actionSide}`);
            if (cell) {
                cell.classList.add(leg.action === 'BUY' ? 'cell-selected-ask' : 'cell-selected-bid');
            }
        }
    }

    // The strikes inside the 5-sigma (+/- 60) window around spot, plus the bounds used for the label.
    function _visibleStrikes() {
        const allStrikes = Object.keys(state.chainData).map(Number).sort((a, b) => a - b);
        const spot = state.chainMeta ? state.chainMeta.spot_price : state.currentSpot;
        const annualVol = (state.chainMeta && state.chainMeta.annual_vol) ? state.chainMeta.annual_vol : 0.20;
        const dailyStd = (spot > 0) ? (spot * annualVol / Math.sqrt(252)) : 0;
        const lowerBound = spot > 0 ? (spot - 5 * dailyStd - 60) : -Infinity;
        const upperBound = spot > 0 ? (spot + 5 * dailyStd + 60) : Infinity;
        return { strikes: allStrikes.filter(s => s >= lowerBound && s <= upperBound), lowerBound, upperBound, spot };
    }

    function _createRow(strike) {
        const tr = document.createElement('tr');
        tr.dataset.strike = String(strike);
        const cells = {};
        const addCells = (side, fields) => {
            for (const f of fields) {
                const td = document.createElement('td');
                const key = `${side}_${f}`;
                td.id = `chain_${strike}_${key}`;
                const sa = SIDE_ACTION[key];
                if (sa) {                       // bid/ask cells are clickable: the delegated handler reads these
                    td.dataset.right = sa[0];
                    td.dataset.action = sa[1];
                    td.title = `${sa[1] === 'BUY' ? 'Buy' : 'Sell'} ${strike}${sa[0]}`;
                }
                cells[key] = td;
                tr.appendChild(td);
            }
        };
        addCells('call', CALL_FIELDS);
        cells.strike = document.createElement('td');
        tr.appendChild(cells.strike);
        addCells('put', PUT_FIELDS);
        return { tr, cells, strikeHtml: '' };
    }

    function _sideStale(strike, right, now) {
        const maxAge = state.chainMeta && state.chainMeta.max_age_s;
        const seen = state.chainSideSeenMs[`${strike}|${right}`];
        return !!(maxAge && seen !== undefined && (now - seen) / 1000 > maxAge);
    }

    // Bring one row's text and classes in line with the model; every write is skipped when unchanged.
    function _fillRow(strike, entry, ctx) {
        const r = state.chainData[strike] || {};
        for (const [side, right, fields] of CHAIN_SIDES) {
            const itm = (ctx.spot > 0 && (right === 'C' ? strike < ctx.spot : strike > ctx.spot)) ? `itm-${side}` : '';
            const stale = _sideStale(strike, right, ctx.now);
            chainView.staleState.set(`${strike}|${right}`, stale);
            for (const f of fields) {
                const key = `${side}_${f}`;
                const td = entry.cells[key];
                const txt = formatChainVal(f, r[key]);
                if (td.textContent !== txt) td.textContent = txt;
                const cls = [itm, stale ? 'quote-stale' : ''];
                if (f === 'ask') cls.push('cell-ask', hasSelectedLeg(strike, right, 'BUY') ? 'cell-selected-ask' : '');
                if (f === 'bid') cls.push('cell-bid', hasSelectedLeg(strike, right, 'SELL') ? 'cell-selected-bid' : '');
                const className = cls.filter(Boolean).join(' ');
                if (td.className !== className) td.className = className;
            }
        }
        const rowClasses = [];
        let tags = '';
        if (strike === ctx.atm) rowClasses.push('row-atm');
        if (ctx.cw !== null && strike === ctx.cw) { rowClasses.push('row-call-wall'); tags += '<span class="strike-tag strike-tag-cw">CW</span>'; }
        if (ctx.pw !== null && strike === ctx.pw) { rowClasses.push('row-put-wall'); tags += '<span class="strike-tag strike-tag-pw">PW</span>'; }
        if (ctx.gf !== null && Math.abs(strike - ctx.gf) < 2.5) { rowClasses.push('row-gamma-flip'); tags += '<span class="strike-tag strike-tag-gf">GF</span>'; }
        const rowClass = rowClasses.join(' ');
        if (entry.tr.className !== rowClass) entry.tr.className = rowClass;
        const strikeCell = entry.cells.strike;
        const strikeHtml = `${strike}${tags}`;
        if (entry.strikeHtml !== strikeHtml) { strikeCell.innerHTML = strikeHtml; entry.strikeHtml = strikeHtml; }
        const strikeClass = `strike-col ${getStrikeSigmaClass(r.sigma_distance_abs)}`.trim();
        if (strikeCell.className !== strikeClass) strikeCell.className = strikeClass;
        const title = buildStrikeSigmaTitle(strike, r.sigma_distance_signed, r.sigma_distance_abs, ctx.spot);
        if (strikeCell.title !== title) strikeCell.title = title;
    }

    // Insert/remove only the rows whose strike entered/left the window, then refresh the row tags and
    // classes. The strike at the viewport centre keeps its on-screen position so the table never jumps.
    function renderChainStructure() {
        const tbody = document.getElementById('chainBody');
        const wrap = document.getElementById('chainTableWrap');
        const { strikes, lowerBound, upperBound, spot } = _visibleStrikes();
        if (strikes.length === 0) {
            chainView.rows.clear();
            chainView.dirty.clear();
            chainView.staleState.clear();
            chainView.built = false;
            tbody.innerHTML = '<tr><td colspan="19" style="text-align:center;color:#475569;padding:40px;">Waiting for option chain data...</td></tr>';
            document.getElementById('chainRangeInfo').textContent = 'Visible range: -';
            return;
        }

        let anchorStrike = null;
        let anchorOffset = 0;
        if (chainView.built) {
            anchorStrike = getChainViewportCenterStrike();
            const anchor = anchorStrike !== null ? chainView.rows.get(anchorStrike) : undefined;
            if (anchor) anchorOffset = anchor.tr.offsetTop - wrap.scrollTop;
            else anchorStrike = null;
        } else {
            tbody.innerHTML = '';               // drop the "Waiting for option chain data..." row
        }

        const keep = new Set(strikes);
        for (const [s, e] of chainView.rows) {
            if (!keep.has(s)) { e.tr.remove(); chainView.rows.delete(s); }
        }
        let prev = null;
        for (const s of strikes) {
            let e = chainView.rows.get(s);
            if (!e) { e = _createRow(s); chainView.rows.set(s, e); }
            const expected = prev ? prev.tr.nextSibling : tbody.firstChild;
            if (e.tr !== expected) tbody.insertBefore(e.tr, expected);
            prev = e;
        }

        const meta = state.chainMeta;
        const ctx = {
            now: Date.now(),
            spot,
            atm: spot > 0 ? strikes.reduce((a, b) => Math.abs(a - spot) < Math.abs(b - spot) ? a : b) : null,
            cw: meta ? meta.call_wall : null,
            pw: meta ? meta.put_wall : null,
            gf: meta ? meta.gamma_flip : null,
        };
        for (const s of strikes) _fillRow(s, chainView.rows.get(s), ctx);
        chainView.dirty.clear();                // every cell was just written from the model
        document.getElementById('chainRangeInfo').textContent =
            `Visible range: ${Math.round(lowerBound)} to ${Math.round(upperBound)} (5sigma +/- 60)`;

        const first = !chainView.built;
        chainView.built = true;
        if (first) scrollToATM();
        else if (anchorStrike !== null) wrap.scrollTop = chainView.rows.get(anchorStrike).tr.offsetTop - anchorOffset;
        if (state.activeTab === 'chain') reportChainViewportCenter(true);
    }

    // Toggle quote-stale only on the sides whose state changed (1 s timer in main.js). A side is stale
    // when its last tick, or the receipt time seeded from the full payload's age, is older than max_age_s.
    function refreshStaleMarks() {
        if (state.activeTab !== 'chain' || !chainView.built) return;
        const now = Date.now();
        for (const [strike, entry] of chainView.rows) {
            for (const [side, right, fields] of CHAIN_SIDES) {
                const k = `${strike}|${right}`;
                const stale = _sideStale(strike, right, now);
                if (chainView.staleState.get(k) === stale) continue;
                chainView.staleState.set(k, stale);
                for (const f of fields) entry.cells[`${side}_${f}`].classList.toggle('quote-stale', stale);
            }
        }
    }

    // One delegated handler on <tbody>: bid/ask cells carry data-right and data-action.
    function onChainBodyClick(e) {
        const td = e.target.closest('td[data-action]');
        if (!td) return;
        addLeg(Number(td.parentElement.dataset.strike), td.dataset.right, td.dataset.action);
    }

    function getStrikeSigmaClass(sigmaAbs) {
        if (sigmaAbs === null || sigmaAbs === undefined || !isFinite(sigmaAbs)) return '';
        if (sigmaAbs <= 1) return 'strike-sigma-1';
        if (sigmaAbs <= 2) return 'strike-sigma-2';
        if (sigmaAbs <= 3) return 'strike-sigma-3';
        return '';
    }

    function buildStrikeSigmaTitle(strike, sigmaSigned, sigmaAbs, spot) {
        let signed = sigmaSigned;
        let abs = sigmaAbs;
        if ((signed === null || signed === undefined || !isFinite(signed)) &&
            state.chainMeta && state.chainMeta.sigma_move && state.chainMeta.sigma_move > 0 && spot > 0) {
            signed = (strike - spot) / state.chainMeta.sigma_move;
            abs = Math.abs(signed);
        }
        if (abs === null || abs === undefined || !isFinite(abs)) {
            return `Strike ${strike} (sigma distance unavailable)`;
        }
        const signPrefix = signed >= 0 ? '+' : '';
        const direction = signed >= 0 ? 'above' : 'below';
        return `Strike ${strike}: ${signPrefix}${signed.toFixed(2)} sigma (${abs.toFixed(2)} sigma ${direction} spot ${spot.toFixed(2)})`;
    }

    function scrollToATM() {
        const spot = state.chainMeta ? state.chainMeta.spot_price : state.currentSpot;
        if (spot <= 0) return;
        const wrap = document.getElementById('chainTableWrap');
        const row = wrap.querySelector('tr.row-atm');
        if (row) {
            // Scroll so ATM is roughly centered
            const rowTop = row.offsetTop;
            const wrapH = wrap.clientHeight;
            wrap.scrollTop = rowTop - wrapH / 2 + row.clientHeight / 2;
            reportChainViewportCenter(true);
        }
    }

    function getChainViewportCenterStrike() {
        const wrap = document.getElementById('chainTableWrap');
        if (!wrap) return null;

        const rows = wrap.querySelectorAll('tbody tr[data-strike]');
        if (!rows || rows.length === 0) return null;

        const centerY = wrap.scrollTop + (wrap.clientHeight / 2);
        let bestStrike = null;
        let bestDist = Number.POSITIVE_INFINITY;

        for (const row of rows) {
            const strike = Number(row.getAttribute('data-strike'));
            if (!Number.isFinite(strike)) continue;
            const rowCenterY = row.offsetTop + (row.offsetHeight / 2);
            const dist = Math.abs(rowCenterY - centerY);
            if (dist < bestDist) {
                bestDist = dist;
                bestStrike = strike;
            }
        }

        return Number.isFinite(bestStrike) ? bestStrike : null;
    }

    function flushViewportCenterSend(force = false) {
        if (state.chainViewportPendingStrike === null) return;
        if (!(ws && ws.readyState === WebSocket.OPEN)) return;
        if (state.activeTab !== 'chain') return;

        const strike = state.chainViewportPendingStrike;
        if (!Number.isFinite(strike) || strike <= 0) return;
        if (!force && state.chainViewportCenterStrike === strike) return;

        if (!force && Number.isFinite(state.chainViewportCenterStrike) &&
            strike < state.chainViewportCenterStrike &&
            (state.chainViewportCenterStrike - strike) < CHAIN_VIEWPORT_CENTER_THRESHOLD) {
            return;
        }

        state.chainViewportCenterStrike = strike;
        state.chainViewportLastSentMs = Date.now();
        ws.send(`viewport_center:${strike.toFixed(1)}`);
    }

    function reportChainViewportCenter(force = false) {
        if (state.activeTab !== 'chain') return;

        const strike = getChainViewportCenterStrike();
        if (!Number.isFinite(strike) || strike <= 0) return;

        state.chainViewportPendingStrike = strike;

        const now = Date.now();
        const elapsed = now - state.chainViewportLastSentMs;
        if (force || elapsed >= CHAIN_VIEWPORT_SEND_THROTTLE_MS) {
            if (state.chainViewportSendTimer) {
                clearTimeout(state.chainViewportSendTimer);
                state.chainViewportSendTimer = null;
            }
            flushViewportCenterSend(force);
            return;
        }

        if (state.chainViewportSendTimer) return;
        state.chainViewportSendTimer = setTimeout(() => {
            state.chainViewportSendTimer = null;
            flushViewportCenterSend(false);
        }, CHAIN_VIEWPORT_SEND_THROTTLE_MS - elapsed);
    }

    // ======================================================================
