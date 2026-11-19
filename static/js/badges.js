    // Badge updates
    // ======================================================================
    function fmtGex(val) {
        // GEX is dollars of SPX dealers trade per 1% move; show it as $ in billions (B), millions (M) or thousands (K)
        if (val == null) return '-';
        const abs = Math.abs(val);
        const sign = val < 0 ? '-$' : '+$';
        if (abs >= 1e9)  return sign + (abs / 1e9).toFixed(2) + 'B';
        if (abs >= 1e6)  return sign + (abs / 1e6).toFixed(1) + 'M';
        if (abs >= 1e3)  return sign + (abs / 1e3).toFixed(0) + 'K';
        return sign + abs.toFixed(0);
    }

    // The dealers' regime: true = long gamma (converging), false = short (diverging), null = unknown. The
    // dealer block's regime (gamma at spot) wins; without it, the sign of the total net GEX decides.
    function gexLongGamma(g) {
        if (!g) return null;
        const regime = g.dealer ? g.dealer.regime : null;
        if (regime === 'long_gamma') return true;
        if (regime === 'short_gamma') return false;
        const net = g.total_net_gex;
        return net == null ? null : net >= 0;
    }

    // A dollar badge (Charm to close, Vanna): converging when the value is >= 0, diverging below; hidden when null.
    function _setFlowBadge(boxId, valId, val, title) {
        const box = document.getElementById(boxId);
        if (!box) return;
        if (val == null) { box.style.display = 'none'; return; }
        box.style.display = '';
        box.classList.toggle('badge-converging', val >= 0);
        box.classList.toggle('badge-diverging', val < 0);
        box.title = title;
        document.getElementById(valId).textContent = fmtGex(val);
    }

    function updateBadges() {
        const spot = priceChart.latest ? priceChart.latest.close : null;   // latest bar of the shown series
        document.getElementById('spotBadge').textContent = spot ? spot.toFixed(2) : '-';

        if (state.gex) {
            const g = state.gex;
            const dealer = g.dealer || null;
            document.getElementById('callWallBadge').textContent =
                g.call_wall != null ? g.call_wall.toFixed(0) : '-';
            document.getElementById('putWallBadge').textContent =
                g.put_wall != null ? g.put_wall.toFixed(0) : '-';
            document.getElementById('gammaFlipBadge').textContent =
                g.gamma_flip != null ? g.gamma_flip.toFixed(1) : '-';
            const zgTitle = 'Zero gamma (ZG): the spot where total dealer gamma changes sign, searched within +/-5% of spot; "-" when there is none.';
            document.querySelector('.badge-gamma-flip').title = g.gamma_flip_cumulative != null
                ? `${zgTitle} Old cumulative flip: ${Number(g.gamma_flip_cumulative).toFixed(1)}`
                : zgTitle;
            document.getElementById('maxPainBadge').textContent =
                g.max_pain != null ? g.max_pain.toFixed(0) : '-';

            // Net GEX value
            const netGex = g.total_net_gex;
            document.getElementById('netGexBadge').textContent = fmtGex(netGex);

            // MM Hedging regime
            const regimeBadge = document.getElementById('mmRegimeBadge');
            const regimeText  = document.getElementById('mmRegimeText');
            const longGamma = gexLongGamma(g);
            if (longGamma != null) {
                regimeBadge.style.display = '';
                if (longGamma) {
                    regimeBadge.className = 'level-badge badge-converging';
                    regimeText.innerHTML  = '- MM: CONVERGING';
                    regimeText.title      = dealer
                        ? 'Dealer gamma at spot from the gamma profile (G(spot)) is positive: dealers are net long gamma. They sell into rallies and buy dips - market-stabilising.'
                        : 'Positive GEX: dealers are net long gamma. They sell into rallies and buy dips - market-stabilising.';
                } else {
                    regimeBadge.className = 'level-badge badge-diverging';
                    regimeText.innerHTML  = '- MM: DIVERGING';
                    regimeText.title      = dealer
                        ? 'Dealer gamma at spot from the gamma profile (G(spot)) is negative: dealers are net short gamma. They chase moves in the same direction - market-amplifying.'
                        : 'Negative GEX: dealers are net short gamma. They chase moves in the same direction - market-amplifying.';
                }
            } else {
                regimeBadge.style.display = 'none';
            }

            // Expected move (ATM straddle at the open): the badge shows the open half-width; the title the anchored band
            const em = dealer ? dealer.em : null;
            const emBox = document.getElementById('emBadgeBox');
            if (em && em.open != null) {
                emBox.style.display = '';
                document.getElementById('emBadge').textContent = '±' + Math.round(em.open);
                const band = (em.high != null && em.low != null)
                    ? `anchored band ${Number(em.low).toFixed(2)} to ${Number(em.high).toFixed(2)}` : 'anchored band not set';
                const anchor = em.anchor_time ? `anchored at ${em.anchor_time} ET` : 'not anchored yet';
                const live = em.live != null ? `live ±${Number(em.live).toFixed(1)}` : 'live not known';
                emBox.title = `Expected move: ±${Number(em.open).toFixed(1)} from the ATM straddle at the open (${band}; ${anchor}; ${live}).`;
            } else {
                emBox.style.display = 'none';
            }

            // Charm to close and vanna (dollar flows of the dealers; see the signal rules in the README)
            const charm = dealer ? dealer.charm : null;
            _setFlowBadge('charmCloseBadgeBox', 'charmCloseBadge', charm ? charm.to_close_usd : null,
                'Charm to close: the underlying the dealers buy (+) or sell (-) by the close as time passes, at constant spot and vol.');
            const vanna = dealer ? dealer.vanna : null;
            _setFlowBadge('vannaBadgeBox', 'vannaBadge', vanna ? vanna.usd_per_vol_down : null,
                'Vanna: the underlying the dealers buy (+) or sell (-) if implied vol falls one point, at constant spot.');
            // Customer flow the dealers hedged over the last 30 minutes (hidden until the flow tracker has data)
            const flow = dealer ? dealer.flow : null;
            const flowSince = flow && flow.coverage_start ? `; coverage from ${flow.coverage_start} ET` : '';
            _setFlowBadge('flow30BadgeBox', 'flow30Badge', flow ? flow.hedge_pressure_30m_usd : null,
                `Hedge flow 30m: the underlying the dealers bought (+) or sold (-) against customer trades in the last 30 minutes${flowSince}.`);
        }
    }

    function updateStatus(data) {
        // Connection dot
        const dot = document.getElementById('connDot');
        const connText = document.getElementById('connStatus');
        if (data.connected) {
            dot.className = 'dot dot-green';
            connText.textContent = 'IB Connected';
            document.getElementById('loadingOverlay').classList.add('hidden');
        } else {
            dot.className = 'dot dot-red';
            connText.textContent = 'IB Disconnected';
        }

        // Market status
        const ms = document.getElementById('marketStatus');
        const msVal = data.market_status || '-';
        ms.textContent = msVal;
        if (msVal === 'RTH') ms.style.color = 'var(--up)';
        else if (msVal === 'GTH' || msVal === 'CURB') ms.style.color = 'var(--accent)';
        else ms.style.color = 'var(--text-muted)';

        // Show/hide Extended Hours badge in order entry row
        const rthBadge = document.getElementById('outsideRthBadge');
        if (rthBadge) rthBadge.style.display = (msVal === 'GTH' || msVal === 'CURB') ? '' : 'none';

        // Expiration
        document.getElementById('expDisplay').textContent = data.expiration || '-';

        // Last GEX update
        if (data.last_chain_update) {
            document.getElementById('lastGexUpdate').textContent = data.last_chain_update;
        }

        // Data mode badge & chart title
        if (data.data_mode) {
            state.dataMode = data.data_mode;
            state.historicalDate = data.historical_date || '';
            updateModeBadge();
        }

        // Update ES-derived spot in real-time (between chain fetches)
        if (data.spot_price > 0) {
            const prevSpot = state.currentSpot;
            const prevDerived = state.esDerived;
            state.currentSpot = data.spot_price;
            state.esDerived = data.es_derived || false;
            // Move the spot lines (the GEX profile's NOW line, the smile's shapes) if spot or regime changed
            if (state.gexProfileReady && state.gex &&
                (state.currentSpot !== prevSpot || state.esDerived !== prevDerived)) {
                requestSpotLineRender();
            }
        }

        // Sync loading overlay with chain_fetching flag
        // (mainly for reconnects where the client missed the starting event)
        if (data.chain_fetching === true && !state.gex) {
            const gexOverlay = document.getElementById('gexLoading');
            const smileOverlay = document.getElementById('smileLoading');
            document.getElementById('gexLoadingText').textContent = 'Fetching option chain-';
            document.getElementById('gexLoadingSub').textContent = '';
            document.getElementById('smileLoadingText').textContent = 'Fetching option chain-';
            document.getElementById('smileLoadingSub').textContent = '';
            gexOverlay.classList.remove('hidden');
            smileOverlay.classList.remove('hidden');
        }
    }

    function updateModeBadge() {
        const badge = document.getElementById('modeBadge');
        const text  = document.getElementById('modeBadgeText');
        const title = document.getElementById('priceChartTitle');

        if (state.dataMode === 'live') {
            badge.style.display = '';
            badge.className = 'level-badge badge-mode-live';
            text.textContent = '\u25CF LIVE';
            title.textContent = 'SPX Intraday \u2014 Live';
        } else if (state.dataMode === 'historical') {
            badge.style.display = '';
            badge.className = 'level-badge badge-mode-hist';
            text.textContent = '\u25CB HISTORICAL ' + state.historicalDate;
            title.textContent = 'SPX Intraday \u2014 ' + state.historicalDate + ' (last session)';
        } else {
            badge.style.display = 'none';
            title.textContent = 'SPX Intraday';
        }
    }

    // ======================================================================
