    // Vertical GEX profile: the gamma exposure by strike drawn as horizontal bars in a column right of the
    // SPX chart. The column shares the chart's price axis (every strike sits at the same height as its price
    // on the candles), so the bars read directly against the live quote. PUT bars grow left and CALL bars
    // right from a center axis ("Split"), or one net bar per strike ("Net"). "Flow" shows the customers' net
    // delta dollars per strike instead (0DTE only). Drag, wheel or the buttons zoom
    // and pan the shared price scale, which scales the candles and the profile together.
    // Colors come from theme tokens (themeColors()), read at draw time.
    // ======================================================================
    const GEX_VIEW_KEY = 'spx-gex-profile-view';     // localStorage: 'split' | 'net' | 'flow'
    const GEX_HOT_KEY = 'spx-gex-hot';               // localStorage: '1' | '0'; the hot-strike dots, on by default
    const GEX_TICK_KEY = 'spx-gex-flow-ticks';       // localStorage: '1' | '0'; the flow GEX ticks, on by default
    const GEX_HOT_GUTTER = 10;                       // px: the left gutter that holds the hot-strike dots
    const GEX_HOT_STALE_FRAC = 0.05;                 // a hot strike whose vol_15m is below this share of its day volume is stale (outlined)
    const GEX_HEAD_H = 20;                           // px: the PUT | CALL caption strip at the top
    const GEX_LABEL_ROOM = 46;                       // px kept free at the outer end of a bar for its value
    const GEX_BAR_FILL = 0.74;                       // share of a strike's band the bar occupies
    const GEX_BAR_MAX_H = 18;
    const GEX_ZOOM_BUTTON = 1.5;                     // one click of + / - changes the visible range by this factor
    const GEX_WHEEL_RATE = 0.0016;                   // zoom exponent per wheel delta unit (about 1.17 per notch)
    const GEX_MIN_SPAN = 4;                          // index points: the closest zoom
    const GEX_DRAG_PX = 3;                           // movement below this is a click, not a drag
    const GEX_LABELS_PER_SIDE = 4;                   // value labels when rows are too thin to label them all
    const GEX_CURVE_KEY = 'spx-gex-curve';           // localStorage: '1' | '0'; the gamma curve is on by default
    const GEX_NODE_HALF = 4;                         // px: half the width of a gamma node's diamond

    const gexProfile = {
        host: null, canvas: null, ctx: null, tip: null,
        view: 'split', font: '', dpr: 1,
        w: 0, h: 0, mapKey: '', rows: [],
        hoverY: null, hoverStrike: null, drag: null,
        drawn: 0,                                     // bars painted by the last draw
        draws: 0,                                     // completed draws (tests count them)
        spotY: null,                                  // y of the NOW line in the last draw, null when off screen
        summarySig: '',
        curveOn: true,                                // the gamma curve (Curve button)
        hotOn: true, ticksOn: true,                   // the hot-strike dots (Hot) and the flow GEX ticks (Ticks)
        curveDrawn: 0, nodesDrawn: 0, voidsDrawn: 0, emDrawn: 0,   // per-draw counts of the dealer overlays
        flowBarsDrawn: 0, flowTicksDrawn: 0, hotDrawn: 0,          // per-draw counts of the flow elements
        caption: '',                                  // the header text of the last draw, e.g. 'PUT | CALL'
    };

    // ---------------------------------------------------------------- pure helpers
    // Zoom ``range`` ({from,to}) by ``factor`` (<1 zooms in) keeping ``anchor`` fixed on screen; the span
    // stays within [minSpan, maxSpan]. A range already past a limit (an autoscaled chart can be wider than
    // maxSpan) is never moved the wrong way: zooming out leaves it, zooming in still narrows it.
    function gexZoomRange(range, factor, anchor, minSpan, maxSpan) {
        const span = range.to - range.from;
        if (!(span > 0) || !(factor > 0)) return range;
        const lo = Math.min(minSpan, span);
        const hi = factor > 1 ? Math.max(maxSpan, span) : maxSpan;
        const next = Math.min(hi, Math.max(lo, span * factor));
        const k = next / span;
        const a = anchor >= range.from && anchor <= range.to ? anchor : (range.from + range.to) / 2;
        return { from: a - (a - range.from) * k, to: a + (range.to - a) * k };
    }

    // Dragging the profile ``dyPx`` down shows higher prices (the content follows the pointer).
    function gexPanRange(range, dyPx, heightPx) {
        if (!(heightPx > 0)) return range;
        const dp = dyPx / heightPx * (range.to - range.from);
        return { from: range.from + dp, to: range.to + dp };
    }

    // The range that shows the spot, the four levels and every price in ``extra`` with a margin, or null
    // when there is nothing to show.
    function gexLevelsRange(g, spot, extra) {
        const levelVals = PRICE_LEVELS.filter(lv => lv.profile !== false).map(lv => g && g[lv.key]);
        const vals = [spot].concat(levelVals)
            .concat(extra || []).filter(v => typeof v === 'number' && isFinite(v) && v > 0);
        if (!vals.length) return null;
        const lo = Math.min(...vals), hi = Math.max(...vals);
        const pad = Math.max((hi - lo) * 0.06, GEX_MIN_SPAN);
        return { from: lo - pad, to: hi + pad };
    }

    // Each strike's band: halfway to its neighbours (a lone strike gets none).
    function gexBands(strikes) {
        const out = [];
        for (let i = 0; i < strikes.length; i++) {
            const s = strikes[i];
            const prev = i > 0 ? s - strikes[i - 1] : (i + 1 < strikes.length ? strikes[i + 1] - s : 0);
            const next = i + 1 < strikes.length ? strikes[i + 1] - s : prev;
            out.push({ strike: s, lo: s - prev / 2, hi: s + next / 2 });
        }
        return out;
    }

    function gexCompact(n) {
        if (n == null || !isFinite(n)) return '-';
        const a = Math.abs(n);
        if (a >= 1e6) return (n / 1e6).toFixed(1) + 'M';
        if (a >= 1e3) return (n / 1e3).toFixed(a >= 1e4 ? 0 : 1) + 'K';
        return String(Math.round(n));
    }

    // The gamma curve as canvas points: the value at each grid price mapped to x around the center line
    // (half), scaled so that |maxAbs| reaches maxLen px. No scale when maxAbs is 0 (every x is the center).
    function gexCurveXY(values, from, step, maxAbs, half, maxLen) {
        const k = maxAbs > 0 ? maxLen / maxAbs : 0;
        return values.map((v, i) => ({ p: from + i * step, x: half + v * k }));
    }

    // The curve's value at price x (linear between grid points), or null outside the grid.
    function gexInterp(from, step, values, x) {
        const n = values ? values.length : 0;
        if (!n || !(step > 0) || !isFinite(from) || !isFinite(x)) return null;
        const t = (x - from) / step;
        if (t < -1e-9 || t > n - 1 + 1e-9) return null;
        const tc = Math.min(Math.max(t, 0), n - 1);
        const i = Math.min(Math.floor(tc), n - 1);
        if (i >= n - 1) return values[n - 1];
        return values[i] + (values[i + 1] - values[i]) * (tc - i);
    }

    // True when the span between two y coordinates (either order) reaches the drawn area below the caption.
    function _gexSpanVisible(y1, y2, H) {
        return Math.max(y1, y2) >= GEX_HEAD_H && Math.min(y1, y2) <= H;
    }

    // ---------------------------------------------------------------- gamma curve (Curve button)
    function setGexCurve(on) {
        gexProfile.curveOn = !!on;
        try { localStorage.setItem(GEX_CURVE_KEY, gexProfile.curveOn ? '1' : '0'); } catch (e) { /* not remembered */ }
        const btn = document.getElementById('gexCurveToggle');
        if (btn) btn.classList.toggle('active', gexProfile.curveOn);
        renderWhenVisible('dashboard', 'gex.curve', drawGexProfile);
    }
    function toggleGexCurve() { setGexCurve(!gexProfile.curveOn); }

    // ---------------------------------------------------------------- hot strikes and flow ticks (Hot, Ticks)
    function _syncGexSwitch(id, on) {
        const btn = document.getElementById(id);
        if (btn) btn.classList.toggle('active', !!on);
    }
    function setGexHot(on) {
        gexProfile.hotOn = !!on;
        try { localStorage.setItem(GEX_HOT_KEY, gexProfile.hotOn ? '1' : '0'); } catch (e) { /* not remembered */ }
        _syncGexSwitch('gexHotToggle', gexProfile.hotOn);
        renderWhenVisible('dashboard', 'gex.hot', drawGexProfile);
    }
    function toggleGexHot() { setGexHot(!gexProfile.hotOn); }
    function setGexTicks(on) {
        gexProfile.ticksOn = !!on;
        try { localStorage.setItem(GEX_TICK_KEY, gexProfile.ticksOn ? '1' : '0'); } catch (e) { /* not remembered */ }
        _syncGexSwitch('gexTickToggle', gexProfile.ticksOn);
        renderWhenVisible('dashboard', 'gex.ticks', drawGexProfile);
    }
    function toggleGexTicks() { setGexTicks(!gexProfile.ticksOn); }

    // ---------------------------------------------------------------- price scale access
    function _gexScale() {
        return priceChart.chart ? priceChart.chart.priceScale('right') : null;
    }

    function _gexPaneHeight() {
        try { return Math.floor(priceChart.chart.panes()[0].getHeight()); } catch (e) { return 0; }
    }

    function _gexSpan(g) {
        const strikes = g && g.gex_bars ? g.gex_bars.map(b => b.strike) : [];
        const spot = spotLevel(g || {});
        const width = strikes.length ? Math.max(...strikes) - Math.min(...strikes) : 0;
        return { min: GEX_MIN_SPAN, max: Math.max(width * 1.2, spot * 0.05, GEX_MIN_SPAN * 4) };
    }

    // Zoom the shared price scale; the live quote (or the middle, when the quote is off screen) stays put.
    function gexZoomBy(factor, anchor) {
        const ps = _gexScale();
        const r = ps && ps.getVisibleRange();
        if (!r) return;
        const g = currentGexData();
        const lim = _gexSpan(g);
        const a = anchor != null ? anchor : spotLevel(g || {});
        ps.setVisibleRange(gexZoomRange(r, factor, a, lim.min, lim.max));
    }
    function gexZoomIn() { gexZoomBy(1 / GEX_ZOOM_BUTTON); }
    function gexZoomOut() { gexZoomBy(GEX_ZOOM_BUTTON); }

    function gexZoomFit() {
        const ps = _gexScale();
        if (ps) ps.setAutoScale(true);
    }

    function gexZoomLevels() {
        const ps = _gexScale();
        const g = currentGexData();
        if (!ps || !g) return;
        const extra = [];
        for (const b of priceChart.bars.values()) extra.push(b.high, b.low);
        const r = gexLevelsRange(g, spotLevel(g), extra);
        if (r) ps.setVisibleRange(r);
    }

    // ---------------------------------------------------------------- view mode
    function setGexView(view) {
        if (view !== 'split' && view !== 'net' && view !== 'flow') return;
        gexProfile.view = view;
        try { localStorage.setItem(GEX_VIEW_KEY, view); } catch (e) { /* storage blocked: not remembered */ }
        document.querySelectorAll('#gexViewToggle .gex-mode-btn').forEach(b => {
            b.classList.toggle('active', b.dataset.view === view);
        });
        drawGexProfile();
    }

    // ---------------------------------------------------------------- summary
    function renderGexSummary(g) {
        const set = (id, text, cls) => {
            const el = document.getElementById(id);
            if (!el) return;
            el.textContent = text;
            el.classList.remove('up', 'down');
            if (cls) el.classList.add(cls);
        };
        const sgn = (v) => (v == null ? '' : (v >= 0 ? 'up' : 'down'));
        const dl = g && g.dealer ? g.dealer : null;
        const sig = g ? [g.timestamp, g.total_net_gex, g.total_call_oi, g.total_put_oi, state.gexMode,
                         state.monthlyExpiration, dl ? dl.regime : '',
                         dl ? [dl.gamma_at_spot, dl.zero_gamma_distance, dl.em && dl.em.used_pct,
                               dl.charm && dl.charm.to_close_usd, dl.vanna && dl.vanna.usd_per_vol_down,
                               dl.flow && dl.flow.hedge_pressure_30m_usd, dl.flow && dl.flow.hedge_pressure_day_usd] : ''].join('|') : '';
        if (sig === gexProfile.summarySig) return;
        gexProfile.summarySig = sig;
        if (!g) {
            set('gexSumNet', '-'); set('gexSumRegime', ''); set('gexSumOi', '-'); set('gexSumPc', '-'); set('gexSumSkew', '-');
            set('gexSumGamma', '-'); set('gexSumZgDist', '-'); set('gexSumEmUsed', '-');
            set('gexSumCharm', '-'); set('gexSumVanna', '-'); set('gexSumFlow30', '-');
            set('gexChartLabel', 'GEX by strike');
            return;
        }
        const net = g.total_net_gex;
        const longGamma = gexLongGamma(g);
        set('gexSumNet', fmtGex(net), net == null ? '' : (net >= 0 ? 'up' : 'down'));
        set('gexSumRegime', longGamma == null ? '' : (longGamma ? 'CONVERGING' : 'DIVERGING'),
            longGamma == null ? '' : (longGamma ? 'up' : 'down'));
        // Dealer rows: "-" in monthly mode and whenever the profile could not be computed.
        const gam = dl ? dl.gamma_at_spot : null;
        set('gexSumGamma', gam == null ? '-' : `${longGamma ? 'Long γ' : 'Short γ'} ${fmtGex(gam)}`, sgn(gam));
        const zgd = dl ? dl.zero_gamma_distance : null;
        set('gexSumZgDist', zgd == null ? '-' : `${zgd > 0 ? '+' : ''}${zgd.toFixed(1)}`);
        const used = dl && dl.em ? dl.em.used_pct : null;
        set('gexSumEmUsed', used == null ? '-' : `${Math.round(used)}%`);
        const charm = dl && dl.charm ? dl.charm.to_close_usd : null;
        set('gexSumCharm', fmtGex(charm), sgn(charm));
        const vanna = dl && dl.vanna ? dl.vanna.usd_per_vol_down : null;
        set('gexSumVanna', fmtGex(vanna), sgn(vanna));
        const flow30 = dl && dl.flow ? dl.flow.hedge_pressure_30m_usd : null;   // '-' in monthly mode and before the tracker has data
        set('gexSumFlow30', fmtGex(flow30), sgn(flow30));
        // The day's total is the flow pane's line (its scale is hidden): the tooltip gives its value.
        const flowDay = dl && dl.flow ? dl.flow.hedge_pressure_day_usd : null;
        const f30 = document.getElementById('gexSumFlow30');
        if (f30) f30.title = flowDay == null ? '' : `Day total ${fmtGex(flowDay)}`;
        const co = g.total_call_oi, po = g.total_put_oi;
        set('gexSumOi', (co > 0 || po > 0) ? `${gexCompact(co)} / ${gexCompact(po)}` : '-');
        if (co > 0) {
            const pc = po / co;
            set('gexSumPc', pc.toFixed(2), pc <= 1 ? 'up' : 'down');
        } else {
            set('gexSumPc', '-');
        }
        const callG = g.total_call_gex || 0, putG = Math.abs(g.total_put_gex || 0);
        if (callG + putG > 0) {
            const pct = callG / (callG + putG) * 100;
            set('gexSumSkew', pct.toFixed(0) + '%', pct >= 50 ? 'up' : 'down');
        } else {
            set('gexSumSkew', '-');
        }
        const e = (state.gexMode === 'monthly' ? state.monthlyExpiration : '') || g.expiration || '';
        const exp = /^\d{8}$/.test(e) ? `${e.slice(0, 4)}-${e.slice(4, 6)}-${e.slice(6, 8)}` : e;
        set('gexChartLabel', exp ? `Exp ${exp}` : 'GEX by strike');
    }

    // ---------------------------------------------------------------- drawing
    function _gexSizeCanvas(w, h) {
        const P = gexProfile;
        const dpr = window.devicePixelRatio || 1;
        const bw = Math.round(w * dpr), bh = Math.round(h * dpr);
        if (P.canvas.width !== bw || P.canvas.height !== bh) { P.canvas.width = bw; P.canvas.height = bh; }
        const css = h + 'px';
        if (P.canvas.style.height !== css) P.canvas.style.height = css;
        P.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        P.dpr = dpr;
        P.w = w; P.h = h;
    }

    function _gexTag(ctx, text, x, y, fg, bg, align) {
        ctx.font = `600 10px ${gexProfile.font}`;
        const w = Math.ceil(ctx.measureText(text).width) + 8;
        const left = align === 'right' ? x - w : x;
        ctx.fillStyle = bg;
        ctx.fillRect(left, y - 7, w, 14);
        ctx.fillStyle = fg;
        ctx.textBaseline = 'middle';
        ctx.textAlign = 'left';
        ctx.fillText(text, left + 4, y + 0.5);
    }

    function drawGexProfile() {
        const P = gexProfile;
        if (!P.canvas || !priceChart.candles) return;
        const W = P.host.clientWidth;
        const H = _gexPaneHeight();
        if (!(W > 0) || !(H > 0)) return;                      // hidden tab, or the chart is not laid out yet
        _gexSizeCanvas(W, H);
        const ctx = P.ctx;
        ctx.clearRect(0, 0, W, H);
        P.rows = [];
        P.drawn = 0;
        P.curveDrawn = 0;
        P.nodesDrawn = 0;
        P.voidsDrawn = 0;
        P.emDrawn = 0;
        P.flowBarsDrawn = 0;
        P.flowTicksDrawn = 0;
        P.hotDrawn = 0;
        P.spotY = null;
        P.draws++;
        const ps = _gexScale();
        const range = ps && ps.getVisibleRange();
        P.mapKey = _gexMapKey(range, H, W);
        const fit = document.getElementById('gexZoomFit');
        if (fit && ps) fit.classList.toggle('active', !!ps.options().autoScale);

        const c = themeColors();
        const g = currentGexData();
        const monthly = state.gexMode === 'monthly';          // no dealer block, no flow: the Flow view draws Split
        const dealer = g && g.dealer ? g.dealer : null;       // monthly mode has no dealer block: no overlays
        const flow = !monthly && dealer && dealer.flow ? dealer.flow : null;
        const mode = P.view === 'flow' && monthly ? 'split' : P.view;
        const flowView = mode === 'flow';
        const net = mode === 'net';
        const toY = (p) => priceChart.candles.priceToCoordinate(p);
        const half = W / 2;

        ctx.save();
        ctx.beginPath();
        ctx.rect(0, GEX_HEAD_H, W, H - GEX_HEAD_H);
        ctx.clip();

        if (!g || !g.gex_bars || !g.gex_bars.length || !range) {
            ctx.fillStyle = c.textFaint;
            ctx.font = `12px ${P.font}`;
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            ctx.fillText('Waiting for GEX data', half, H / 2);
            ctx.restore();
            _gexHeader(ctx, W, c, mode);
            return;
        }

        // Rows: one per strike whose band touches the visible area. A row carries the flow keys only when the
        // bar has them (a strike with no customer flow, or monthly mode, has none).
        const cov = flow && flow.coverage ? flow.coverage : null;
        const inCoverage = (strike) => !cov || cov.lo == null || cov.hi == null || (strike >= cov.lo && strike <= cov.hi);
        const bars = g.gex_bars.filter(b => b.strike != null).sort((a, b) => a.strike - b.strike);
        const bands = gexBands(bars.map(b => b.strike));
        const rows = [];
        for (let i = 0; i < bars.length; i++) {
            const b = bars[i], band = bands[i];
            const yHi = toY(band.hi), yLo = toY(band.lo), yc = toY(b.strike);
            if (yHi === null || yLo === null || yc === null) continue;
            if (yLo < GEX_HEAD_H || yHi > H) continue;
            rows.push({ strike: b.strike, top: yHi, bot: yLo, y: yc, h: yLo - yHi,
                        call: b.call_gex || 0, put: b.put_gex || 0, net: b.net_gex || 0,
                        callOi: b.call_oi, putOi: b.put_oi, callVol: b.call_vol, putVol: b.put_vol,
                        callBuy: monthly ? undefined : b.call_buy, callSell: monthly ? undefined : b.call_sell,
                        putBuy: monthly ? undefined : b.put_buy, putSell: monthly ? undefined : b.put_sell,
                        callDelta: monthly ? undefined : b.call_net_delta_usd,
                        putDelta: monthly ? undefined : b.put_net_delta_usd,
                        flowGex: monthly ? undefined : b.flow_gex,
                        volOi: monthly ? undefined : b.vol_oi, hot: !monthly && !!b.hot,
                        vol15m: monthly ? undefined : b.vol_15m, inCov: inCoverage(b.strike)});
        }
        P.rows = rows;

        // The bar scale follows what is on screen, so zooming in enlarges the bars. The Flow view scales its
        // bars to the customer delta dollars instead.
        let maxAbs = 0;
        for (const r of rows) {
            if (flowView) maxAbs = Math.max(maxAbs, Math.abs(r.callDelta || 0), Math.abs(r.putDelta || 0));
            else maxAbs = Math.max(maxAbs, net ? Math.abs(r.net) : Math.max(Math.abs(r.call), Math.abs(r.put)));
        }
        const maxLen = Math.max(8, half - GEX_LABEL_ROOM);
        const lenOf = (v) => (maxAbs > 0 && v ? Math.max(1, Math.abs(v) / maxAbs * maxLen) : 0);

        // Gamma nodes: the rows that carry one (their value labels are drawn with the node marker). The Flow
        // view has no gamma bars, so it has no node markers either.
        const nodes = dealer && !flowView ? (dealer.nodes || []) : [];
        const nodeRows = new Set();
        for (const n of nodes) {
            const r = rows.find(x => Math.abs(x.strike - n.strike) < 0.005);
            if (r) nodeRows.add(r);
        }

        // Voids: bands of strikes with little gamma between them, behind the bars.
        for (const v of (dealer && dealer.voids) || []) {
            const y1 = toY(v.hi), y2 = toY(v.lo);
            if (y1 === null || y2 === null || !_gexSpanVisible(y1, y2, H)) continue;
            const top = Math.max(GEX_HEAD_H, Math.min(y1, y2)), bot = Math.min(H, Math.max(y1, y2));
            ctx.fillStyle = withAlpha(c.textFaint, 0.06);
            ctx.fillRect(0, top, W, bot - top);
            P.voidsDrawn++;
        }

        // Hover band under the bars.
        const hover = P.hoverStrike === null ? null : rows.find(r => r.strike === P.hoverStrike);
        if (hover) {
            ctx.fillStyle = withAlpha(c.textStrong, 0.08);
            ctx.fillRect(0, hover.top, W, Math.max(1, hover.bot - hover.top));
        }

        // Bars.
        const fills = {
            call: withAlpha(c.up, 0.68), put: withAlpha(c.down, 0.68),
            callHi: withAlpha(c.up, 0.95), putHi: withAlpha(c.down, 0.95),
        };
        const labelAll = rows.length > 0 && rows.every(r => r.h >= 12);
        const topBy = (fn) => rows.map(r => ({ r, v: fn(r) })).sort((a, b) => b.v - a.v)
            .slice(0, GEX_LABELS_PER_SIDE).map(x => x.r);
        const ranked = (key) => topBy(r => Math.abs(r[key]));
        const labeled = new Set(flowView
            ? topBy(r => Math.abs(r.callDelta || 0)).concat(topBy(r => Math.abs(r.putDelta || 0)))
            : (net ? ranked('net') : ranked('call').concat(ranked('put'))));
        nodeRows.forEach(r => labeled.add(r));
        ctx.font = `10px ${P.font}`;
        ctx.textBaseline = 'middle';
        for (const r of rows) {
            const bh = Math.max(1, Math.min(GEX_BAR_MAX_H, r.h * GEX_BAR_FILL));
            const top = r.y - bh / 2;
            const hi = hover === r;
            const wantLabel = (labelAll || labeled.has(r) || hi) && bh >= 6 && !nodeRows.has(r);
            if (flowView) {
                if (!r.inCov) {                              // outside the stream coverage: a faint stub, no bar
                    ctx.fillStyle = withAlpha(c.textFaint, 0.5);
                    ctx.fillRect(half - 3, r.y - 1, 6, 2);
                    continue;
                }
                const cl = lenOf(r.callDelta), pl = lenOf(r.putDelta);
                if (cl) { _gexFlowBar(ctx, c.up, half, top, cl, bh, r.callBuy > r.callSell, hi); P.flowBarsDrawn++; }
                if (pl) { _gexFlowBar(ctx, c.down, half - pl, top, pl, bh, r.putBuy > r.putSell, hi); P.flowBarsDrawn++; }
                if (wantLabel) {
                    if (cl) _gexValue(ctx, c, r.callDelta, half + cl + 3, r.y, true);
                    if (pl) _gexValue(ctx, c, r.putDelta, half - pl - 3, r.y, false);
                }
            } else if (net) {
                const len = lenOf(r.net);
                if (!len) continue;
                const pos = r.net >= 0;
                ctx.fillStyle = pos ? (hi ? fills.callHi : fills.call) : (hi ? fills.putHi : fills.put);
                ctx.fillRect(pos ? half : half - len, top, len, bh);
                P.drawn++;
                if (wantLabel) _gexValue(ctx, c, r.net, pos ? half + len + 3 : half - len - 3, r.y, pos);
            } else {
                const cl = lenOf(r.call), pl = lenOf(r.put);
                if (cl) { ctx.fillStyle = hi ? fills.callHi : fills.call; ctx.fillRect(half, top, cl, bh); P.drawn++; }
                if (pl) { ctx.fillStyle = hi ? fills.putHi : fills.put; ctx.fillRect(half - pl, top, pl, bh); P.drawn++; }
                if (wantLabel) {
                    if (cl && Math.abs(r.call) >= maxAbs * 0.08) _gexValue(ctx, c, r.call, half + cl + 3, r.y, true);
                    if (pl && Math.abs(r.put) >= maxAbs * 0.08) _gexValue(ctx, c, r.put, half - pl - 3, r.y, false);
                }
            }
        }

        // Flow GEX ticks (Split and Net): a 2 px tick at the flow gamma's length on the net side of each
        // streamed strike, on the same scale as the GEX bars, so the open-interest and the flow gamma compare.
        if (P.ticksOn && !monthly && !flowView && maxAbs > 0) {
            for (const r of rows) {
                if (typeof r.flowGex !== 'number' || r.flowGex === 0) continue;   // no flow gamma, no tick
                const bh = Math.max(1, Math.min(GEX_BAR_MAX_H, r.h * GEX_BAR_FILL));
                const len = Math.min(maxLen, Math.max(1, Math.abs(r.flowGex) / maxAbs * maxLen));
                const x = r.flowGex >= 0 ? half + len : half - len;
                ctx.fillStyle = c.textMuted;
                ctx.fillRect(x - 1, r.y - bh / 2, 2, bh);
                P.flowTicksDrawn++;
            }
        }

        // Hot strikes (Hot): a dot in the left gutter per hot strike, radius by vol/OI (capped); outlined when
        // its recent volume is stale (little volume added in the last 15 minutes against the day's volume).
        if (P.hotOn && !monthly) {
            for (const r of rows) {
                if (!r.hot) continue;
                const rad = Math.min(4, 1.5 + (r.volOi || 0));
                const x = GEX_HOT_GUTTER / 2;
                ctx.fillStyle = c.accent;
                ctx.beginPath(); ctx.arc(x, r.y, rad, 0, Math.PI * 2); ctx.fill();
                if (typeof r.vol15m === 'number' && r.vol15m < GEX_HOT_STALE_FRAC * ((r.callVol || 0) + (r.putVol || 0))) {
                    ctx.strokeStyle = c.accent; ctx.lineWidth = 1;
                    ctx.beginPath(); ctx.arc(x, r.y, rad + 2, 0, Math.PI * 2); ctx.stroke();
                }
                P.hotDrawn++;
            }
        }

        // Center axis.
        ctx.fillStyle = c.borderStrong;
        ctx.fillRect(Math.round(half), GEX_HEAD_H, 1, H - GEX_HEAD_H);

        // Flow coverage: a thin bracket on the center axis from the lowest to the highest streamed strike.
        if (flowView && cov && cov.lo != null && cov.hi != null) {
            const y1 = toY(cov.hi), y2 = toY(cov.lo);
            if (y1 !== null && y2 !== null && _gexSpanVisible(y1, y2, H)) {
                const top = Math.max(GEX_HEAD_H, Math.min(y1, y2)), bot = Math.min(H, Math.max(y1, y2));
                const x = Math.round(half);
                ctx.fillStyle = c.textFaint;
                ctx.fillRect(x, top, 1, bot - top);
                ctx.fillRect(x - 3, top, 7, 1);
                ctx.fillRect(x - 3, bot - 1, 7, 1);
            }
        }

        // The gamma curve: the dealers' G(price) as a 1 px line, scaled so its largest visible value reaches
        // the bar length. A point counts as drawn when its price is on screen.
        const prof = dealer && dealer.profile && dealer.profile.values ? dealer.profile : null;
        if (prof && P.curveOn) {
            let curveMax = 0;
            prof.values.forEach((v, i) => {
                const y = toY(prof.from + i * prof.step);
                if (y !== null && y >= GEX_HEAD_H && y <= H) curveMax = Math.max(curveMax, Math.abs(v || 0));
            });
            const pts = gexCurveXY(prof.values, prof.from, prof.step, curveMax, half, maxLen);
            ctx.strokeStyle = c.textMuted;
            ctx.lineWidth = 1;
            ctx.setLineDash([]);
            ctx.beginPath();
            let started = false;
            for (const pt of pts) {
                const y = toY(pt.p);
                if (y === null) continue;
                if (y >= GEX_HEAD_H && y <= H) P.curveDrawn++;
                if (started) ctx.lineTo(pt.x, y); else { ctx.moveTo(pt.x, y); started = true; }
            }
            ctx.stroke();
        }

        // Gamma nodes: a diamond at the outer end of the strike's bar, filled for a pin (right side) and
        // hollow for an accel node (left side). Its label and value sit beyond the diamond.
        for (const n of nodes) {
            const r = rows.find(x => Math.abs(x.strike - n.strike) < 0.005);
            if (!r) continue;
            const pin = n.kind === 'pin';
            const outer = pin
                ? half + (net ? (r.net > 0 ? lenOf(r.net) : 0) : lenOf(r.call))
                : half - (net ? (r.net < 0 ? lenOf(r.net) : 0) : lenOf(r.put));
            _gexDiamond(ctx, outer, r.y, GEX_NODE_HALF, pin ? c.info : null, c.alt);
            P.nodesDrawn++;
            const bh = Math.max(1, Math.min(GEX_BAR_MAX_H, r.h * GEX_BAR_FILL));
            if (bh >= 6) {
                const v = net ? r.net : (pin ? r.call : r.put);
                _gexValue(ctx, c, v, pin ? outer + GEX_NODE_HALF + 3 : outer - GEX_NODE_HALF - 3, r.y, pin, n.label);
            }
        }

        const spot = spotLevel(g);

        // Expected move on the right edge: the anchored open band (3 px) and the live straddle band (2 px).
        const em = dealer ? dealer.em : null;
        if (em) {
            if (em.high != null && em.low != null) {
                const y1 = toY(em.high), y2 = toY(em.low);
                if (y1 !== null && y2 !== null && _gexSpanVisible(y1, y2, H)) {
                    ctx.fillStyle = c.info;
                    ctx.fillRect(W - 4, Math.min(y1, y2), 3, Math.abs(y2 - y1));
                    P.emDrawn++;
                }
            }
            if (em.live != null && spot > 0) {
                const y1 = toY(spot + em.live), y2 = toY(spot - em.live);
                if (y1 !== null && y2 !== null && _gexSpanVisible(y1, y2, H)) {
                    ctx.fillStyle = withAlpha(c.info, 0.4);
                    ctx.fillRect(W - 8, Math.min(y1, y2), 2, Math.abs(y2 - y1));
                    P.emDrawn++;
                }
            }
        }

        // Key levels: a dashed line and a tag; a level off screen gets an arrow at the edge instead.
        const edgeTags = [];
        for (const lv of PRICE_LEVELS) {
            if (lv.profile === false) continue;              // expected move and pins have their own profile layer
            const v = g[lv.key];
            if (v == null) continue;
            const y = toY(v);
            if (y === null) continue;
            const color = c[lv.color];
            if (y >= GEX_HEAD_H && y <= H) {
                ctx.strokeStyle = withAlpha(color, 0.85);
                ctx.lineWidth = 1;
                ctx.setLineDash([4, 3]);
                ctx.beginPath(); ctx.moveTo(0, Math.round(y) + 0.5); ctx.lineTo(W, Math.round(y) + 0.5); ctx.stroke();
                ctx.setLineDash([]);
                _gexTag(ctx, lv.tag, 2, y - 8, c.bgPanel, color, 'left');
            } else {
                edgeTags.push({ lv, v, up: y < GEX_HEAD_H, color });
            }
        }

        // The live quote.
        const sy = spot > 0 ? toY(spot) : null;
        const spotColor = state.esDerived ? c.accent : c.textStrong;
        if (sy !== null && sy >= GEX_HEAD_H && sy <= H) {
            P.spotY = sy;
            ctx.strokeStyle = spotColor;
            ctx.lineWidth = 1.5;
            ctx.setLineDash([2, 2]);
            ctx.beginPath(); ctx.moveTo(0, Math.round(sy) + 0.5); ctx.lineTo(W, Math.round(sy) + 0.5); ctx.stroke();
            ctx.setLineDash([]);
            _gexTag(ctx, state.esDerived ? 'ES NOW' : 'NOW', W - 2, sy, c.bgPanel, spotColor, 'right');
        } else if (sy !== null && spot > 0) {
            edgeTags.push({ lv: { tag: state.esDerived ? 'ES NOW' : 'NOW' }, v: spot, up: sy < GEX_HEAD_H, color: spotColor });
        }

        // Off-screen markers, stacked from the edge.
        let topN = 0, botN = 0;
        for (const t of edgeTags) {
            const label = `${t.up ? '▲' : '▼'} ${t.lv.tag} ${_intFmt.format(Math.round(t.v))}`;
            const y = t.up ? GEX_HEAD_H + 9 + 16 * topN++ : H - 9 - 16 * botN++;
            _gexTag(ctx, label, W - 2, y, c.bgPanel, t.color, 'right');
        }

        // Pointer guide with the price under it.
        if (P.hoverY !== null && P.hoverY >= GEX_HEAD_H && P.hoverY <= H) {
            ctx.strokeStyle = withAlpha(c.textMuted, 0.6);
            ctx.lineWidth = 1;
            ctx.setLineDash([1, 3]);
            ctx.beginPath(); ctx.moveTo(0, Math.round(P.hoverY) + 0.5); ctx.lineTo(W, Math.round(P.hoverY) + 0.5); ctx.stroke();
            ctx.setLineDash([]);
            const p = priceChart.candles.coordinateToPrice(P.hoverY);
            if (p !== null) _gexTag(ctx, p.toFixed(1), W - 2, P.hoverY, c.textStrong, c.bgOverlay, 'right');
        }
        ctx.restore();
        _gexHeader(ctx, W, c, mode);
    }

    // A flow bar from x (its left edge) over len px: filled when customers net bought that side, else only
    // outlined (net sold). The fill is the side's token at the bar's alpha; the outline is the token itself.
    function _gexFlowBar(ctx, color, x, top, len, bh, netBought, hi) {
        if (netBought) {
            ctx.fillStyle = withAlpha(color, hi ? 0.95 : 0.68);
            ctx.fillRect(x, top, len, bh);
        } else {
            ctx.strokeStyle = hi ? withAlpha(color, 0.95) : color;
            ctx.lineWidth = 1;
            ctx.strokeRect(x + 0.5, top + 0.5, Math.max(0, len - 1), Math.max(0, bh - 1));
        }
    }

    // A node marker: a diamond of half-size r, filled when fill is given, else stroked in stroke.
    function _gexDiamond(ctx, x, y, r, fill, stroke) {
        ctx.beginPath();
        ctx.moveTo(x, y - r); ctx.lineTo(x + r, y); ctx.lineTo(x, y + r); ctx.lineTo(x - r, y); ctx.closePath();
        if (fill) { ctx.fillStyle = fill; ctx.fill(); return; }
        ctx.strokeStyle = stroke;
        ctx.lineWidth = 1.5;
        ctx.stroke();
    }

    function _gexValue(ctx, c, v, x, y, right, prefix) {
        ctx.fillStyle = c.textMuted;
        ctx.textAlign = right ? 'left' : 'right';
        ctx.fillText((prefix ? prefix + ' ' : '') + fmtGex(v), x, y);
    }

    function _gexHeader(ctx, W, c, mode) {
        const half = W / 2;
        const [left, right] = mode === 'net' ? ['− NET', 'NET +']
            : mode === 'flow' ? ['PUT Δ$', 'CALL Δ$'] : ['PUT', 'CALL'];
        gexProfile.caption = `${left} | ${right}`;
        ctx.fillStyle = c.bgPanel;
        ctx.fillRect(0, 0, W, GEX_HEAD_H);
        ctx.fillStyle = c.border;
        ctx.fillRect(0, GEX_HEAD_H - 1, W, 1);
        ctx.font = `700 10px ${gexProfile.font}`;
        ctx.textBaseline = 'middle';
        ctx.textAlign = 'right';
        ctx.fillStyle = c.down;
        ctx.fillText(left, half - 8, GEX_HEAD_H / 2);
        ctx.textAlign = 'left';
        ctx.fillStyle = c.up;
        ctx.fillText(right, half + 8, GEX_HEAD_H / 2);
    }

    // The map from price to pixel: the visible range, the pane height, the column width, the pixel ratio.
    function _gexMapKey(range, h, w) {
        return range ? [range.from.toFixed(4), range.to.toFixed(4), h, w, window.devicePixelRatio || 1].join('|') : 'none';
    }

    // Called from the chart's own paint: redraw when the price scale, the pane or the column changed, so
    // the bars never lag behind a zoom, a drag on the price axis or a new autoscale.
    function gexProfileSync() {
        const P = gexProfile;
        if (!P.canvas) return;
        const ps = _gexScale();
        const range = ps && ps.getVisibleRange();
        if (_gexMapKey(range, _gexPaneHeight(), P.host.clientWidth) !== P.mapKey) drawGexProfile();
    }

    // Attached to the candle series: its paint hook is how the profile hears about price scale changes
    // (Lightweight Charts has no event for them).
    class GexProfileWatcher {
        constructor(onPaint) {
            const renderer = { draw: () => onPaint() };
            this._views = [{ zOrder: () => 'bottom', renderer: () => renderer }];
        }
        attached() {}
        detached() {}
        paneViews() { return this._views; }
        updateAllViews() {}
    }

    // ---------------------------------------------------------------- pointer
    function _gexRowAt(y) {
        let best = null, bestD = Infinity;
        for (const r of gexProfile.rows) {
            const d = Math.abs(y - r.y);
            const reach = Math.max(r.h / 2, 5);
            if (d <= reach && d < bestD) { best = r; bestD = d; }
        }
        return best;
    }

    function _gexShowTip(row, y) {
        const tip = gexProfile.tip;
        if (!row) { tip.classList.add('hidden'); return; }
        const line = (k, v) => {
            const d = document.createElement('div');
            d.className = 'gex-tip-row';
            const a = document.createElement('span'); a.textContent = k;
            const b = document.createElement('span'); b.textContent = v;
            d.append(a, b);
            return d;
        };
        const title = document.createElement('div');
        title.className = 'gex-tip-title';
        title.textContent = `Strike ${_intFmt.format(row.strike)}`;
        const rowsOut = [title,
            line('Call GEX', fmtGex(row.call)), line('Put GEX', fmtGex(row.put)), line('Net GEX', fmtGex(row.net)),
            line('Call OI', _intFmt.format(row.callOi ?? 0)), line('Put OI', _intFmt.format(row.putOi ?? 0)),
            line('Call Vol', _intFmt.format(row.callVol ?? 0)), line('Put Vol', _intFmt.format(row.putVol ?? 0))];
        // Dealer rows: the node at this strike, and the hedge flow the dealers would trade if spot moved here.
        const dealer = (currentGexData() || {}).dealer || null;
        const node = dealer ? (dealer.nodes || []).find(n => Math.abs(n.strike - row.strike) < 0.005) : null;
        if (node) rowsOut.push(line('Node', `${node.label} ${node.kind}`));
        const prof = dealer && dealer.profile ? dealer.profile : null;
        const hedge = prof ? gexInterp(prof.from, prof.step, prof.hedge, row.strike) : null;
        if (hedge != null) rowsOut.push(line('Hedge $ to here', fmtGex(hedge)));
        // Customer flow and hot strikes: only the rows that carry them (the keys are absent otherwise).
        if (row.callBuy != null) rowsOut.push(line('Call buy / sell', `${_intFmt.format(row.callBuy)} / ${_intFmt.format(row.callSell ?? 0)}`));
        if (row.putBuy != null) rowsOut.push(line('Put buy / sell', `${_intFmt.format(row.putBuy)} / ${_intFmt.format(row.putSell ?? 0)}`));
        if (row.callDelta != null) rowsOut.push(line('Call Δ$', fmtGex(row.callDelta)));
        if (row.putDelta != null) rowsOut.push(line('Put Δ$', fmtGex(row.putDelta)));
        if (row.volOi != null) rowsOut.push(line('Vol/OI', `${Number(row.volOi).toFixed(2)}${row.hot ? ' (hot)' : ''}`));
        if (row.flowGex != null) rowsOut.push(line('Flow GEX', fmtGex(row.flowGex)));
        tip.replaceChildren(...rowsOut);
        tip.classList.remove('hidden');
        const h = tip.offsetHeight;
        tip.style.top = Math.max(GEX_HEAD_H + 4, Math.min(y - h / 2, gexProfile.h - h - 4)) + 'px';
    }

    // The pointer's y in canvas pixels, from the client coordinates and the canvas's rectangle (not
    // offsetY, which a scaled or emulated viewport can skew).
    function _gexY(ev) {
        const r = gexProfile.canvas.getBoundingClientRect();
        return (ev.clientY - r.top) * (r.height > 0 ? gexProfile.h / r.height : 1);
    }

    function _gexPointerMove(ev) {
        const P = gexProfile;
        const y = _gexY(ev);
        if (P.drag) {
            const dy = (ev.clientY - P.drag.y) * (P.h / (P.canvas.getBoundingClientRect().height || P.h));
            if (Math.abs(dy) > GEX_DRAG_PX) P.drag.moved = true;
            if (P.drag.moved) {
                const ps = _gexScale();
                if (ps && P.drag.range) ps.setVisibleRange(gexPanRange(P.drag.range, dy, P.h));
                P.tip.classList.add('hidden');
            }
            return;
        }
        const row = _gexRowAt(y);
        const strike = row ? row.strike : null;
        P.hoverY = y;
        const changed = strike !== P.hoverStrike;
        P.hoverStrike = strike;
        drawGexProfile();
        if (changed || row) _gexShowTip(row, y);
    }

    function _gexPointerLeave() {
        const P = gexProfile;
        if (P.drag) return;
        P.hoverY = null; P.hoverStrike = null;
        P.tip.classList.add('hidden');
        drawGexProfile();
    }

    function _gexPointerDown(ev) {
        if (ev.button !== 0) return;
        const ps = _gexScale();
        gexProfile.drag = { y: ev.clientY, range: ps ? ps.getVisibleRange() : null, moved: false };
        try { gexProfile.canvas.setPointerCapture(ev.pointerId); } catch (e) { /* synthetic event: no capture */ }
        gexProfile.canvas.classList.add('dragging');
    }

    function _gexPointerUp(ev) {
        const P = gexProfile;
        if (!P.drag) return;
        P.drag = null;
        P.canvas.classList.remove('dragging');
        try { P.canvas.releasePointerCapture(ev.pointerId); } catch (e) { /* not captured */ }
    }

    function _gexWheel(ev) {
        ev.preventDefault();                                  // the wheel zooms the price scale, not the page
        const unit = ev.deltaMode === 1 ? 16 : 1;
        const dy = Math.max(-240, Math.min(240, ev.deltaY * unit));
        if (!dy) return;
        const anchor = priceChart.candles.coordinateToPrice(_gexY(ev));
        gexZoomBy(Math.exp(dy * GEX_WHEEL_RATE), anchor);
    }

    // ---------------------------------------------------------------- data job + init
    // Runs when new GEX arrives or the mode changes (a render job, so only while the Dashboard is shown).
    function updateGexProfile() {
        if (!state.gexProfileReady) return;
        renderGexSummary(currentGexData());
        drawGexProfile();
    }

    function initGexProfile() {
        const P = gexProfile;
        P.host = document.getElementById('gexProfile');
        P.canvas = document.getElementById('gexProfileCanvas');
        P.tip = document.getElementById('gexProfileTip');
        if (!P.host || !P.canvas) return;
        if (!priceChart.candles) { P.host.hidden = true; return; }       // no price chart, no shared axis
        P.ctx = P.canvas.getContext('2d');
        P.font = (getComputedStyle(document.documentElement).getPropertyValue('--font-num') || '').trim() || 'monospace';
        let saved = null;
        try { saved = localStorage.getItem(GEX_VIEW_KEY); } catch (e) { /* storage blocked: default */ }
        P.view = saved === 'net' || saved === 'flow' ? saved : 'split';
        let savedCurve = null;
        try { savedCurve = localStorage.getItem(GEX_CURVE_KEY); } catch (e) { /* storage blocked: default on */ }
        P.curveOn = savedCurve !== '0';
        const curveBtn = document.getElementById('gexCurveToggle');
        if (curveBtn) curveBtn.classList.toggle('active', P.curveOn);
        let savedHot = null, savedTicks = null;
        try { savedHot = localStorage.getItem(GEX_HOT_KEY); } catch (e) { /* storage blocked: default on */ }
        try { savedTicks = localStorage.getItem(GEX_TICK_KEY); } catch (e) { /* storage blocked: default on */ }
        P.hotOn = savedHot !== '0';
        P.ticksOn = savedTicks !== '0';
        _syncGexSwitch('gexHotToggle', P.hotOn);
        _syncGexSwitch('gexTickToggle', P.ticksOn);
        document.querySelectorAll('#gexViewToggle .gex-mode-btn').forEach(b => {
            b.classList.toggle('active', b.dataset.view === P.view);
        });
        priceChart.candles.attachPrimitive(new GexProfileWatcher(gexProfileSync));
        P.canvas.addEventListener('pointermove', _gexPointerMove);
        P.canvas.addEventListener('pointerleave', _gexPointerLeave);
        P.canvas.addEventListener('pointerdown', _gexPointerDown);
        P.canvas.addEventListener('pointerup', _gexPointerUp);
        P.canvas.addEventListener('pointercancel', _gexPointerUp);
        P.canvas.addEventListener('dblclick', gexZoomFit);
        P.canvas.addEventListener('wheel', _gexWheel, { passive: false });
        if (window.ResizeObserver) {
            new ResizeObserver(() => scheduleRender('gex.profile.resize', drawGexProfile)).observe(P.host);
        }
        state.gexProfileReady = true;
    }
