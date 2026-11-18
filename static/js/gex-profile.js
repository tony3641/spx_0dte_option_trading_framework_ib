    // Vertical GEX profile: the gamma exposure by strike drawn as horizontal bars in a column right of the
    // SPX chart. The column shares the chart's price axis (every strike sits at the same height as its price
    // on the candles), so the bars read directly against the live quote. PUT bars grow left and CALL bars
    // right from a center axis ("Split"), or one net bar per strike ("Net"). Drag, wheel or the buttons zoom
    // and pan the shared price scale, which scales the candles and the profile together.
    // Colors come from theme tokens (themeColors()), read at draw time.
    // ======================================================================
    const GEX_VIEW_KEY = 'spx-gex-profile-view';     // localStorage: 'split' | 'net'
    const GEX_HEAD_H = 20;                           // px: the PUT | CALL caption strip at the top
    const GEX_LABEL_ROOM = 46;                       // px kept free at the outer end of a bar for its value
    const GEX_BAR_FILL = 0.74;                       // share of a strike's band the bar occupies
    const GEX_BAR_MAX_H = 18;
    const GEX_ZOOM_BUTTON = 1.5;                     // one click of + / - changes the visible range by this factor
    const GEX_WHEEL_RATE = 0.0016;                   // zoom exponent per wheel delta unit (about 1.17 per notch)
    const GEX_MIN_SPAN = 4;                          // index points: the closest zoom
    const GEX_DRAG_PX = 3;                           // movement below this is a click, not a drag
    const GEX_LABELS_PER_SIDE = 4;                   // value labels when rows are too thin to label them all

    const gexProfile = {
        host: null, canvas: null, ctx: null, tip: null,
        view: 'split', font: '', dpr: 1,
        w: 0, h: 0, mapKey: '', rows: [],
        hoverY: null, hoverStrike: null, drag: null,
        drawn: 0,                                     // bars painted by the last draw
        draws: 0,                                     // completed draws (tests count them)
        spotY: null,                                  // y of the NOW line in the last draw, null when off screen
        summarySig: '',
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
        if (view !== 'split' && view !== 'net') return;
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
        const sig = g ? [g.timestamp, g.total_net_gex, g.total_call_oi, g.total_put_oi, state.gexMode,
                         state.monthlyExpiration, g.dealer ? g.dealer.regime : ''].join('|') : '';
        if (sig === gexProfile.summarySig) return;
        gexProfile.summarySig = sig;
        if (!g) {
            set('gexSumNet', '-'); set('gexSumRegime', ''); set('gexSumOi', '-'); set('gexSumPc', '-'); set('gexSumSkew', '-');
            set('gexChartLabel', 'GEX by strike');
            return;
        }
        const net = g.total_net_gex;
        const longGamma = gexLongGamma(g);
        set('gexSumNet', fmtGex(net), net == null ? '' : (net >= 0 ? 'up' : 'down'));
        set('gexSumRegime', longGamma == null ? '' : (longGamma ? 'CONVERGING' : 'DIVERGING'),
            longGamma == null ? '' : (longGamma ? 'up' : 'down'));
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
        P.spotY = null;
        P.draws++;
        const ps = _gexScale();
        const range = ps && ps.getVisibleRange();
        P.mapKey = _gexMapKey(range, H, W);
        const fit = document.getElementById('gexZoomFit');
        if (fit && ps) fit.classList.toggle('active', !!ps.options().autoScale);

        const c = themeColors();
        const g = currentGexData();
        const toY = (p) => priceChart.candles.priceToCoordinate(p);
        const half = W / 2;
        const net = P.view === 'net';

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
            _gexHeader(ctx, W, c, net);
            return;
        }

        // Rows: one per strike whose band touches the visible area.
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
                        callOi: b.call_oi, putOi: b.put_oi, callVol: b.call_vol, putVol: b.put_vol });
        }
        P.rows = rows;

        // The bar scale follows what is on screen, so zooming in enlarges the bars.
        let maxAbs = 0;
        for (const r of rows) {
            maxAbs = Math.max(maxAbs, net ? Math.abs(r.net) : Math.max(Math.abs(r.call), Math.abs(r.put)));
        }
        const maxLen = Math.max(8, half - GEX_LABEL_ROOM);
        const lenOf = (v) => (maxAbs > 0 && v ? Math.max(1, Math.abs(v) / maxAbs * maxLen) : 0);

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
        const ranked = (key) => rows.map(r => ({ r, v: Math.abs(r[key]) })).sort((a, b) => b.v - a.v)
            .slice(0, GEX_LABELS_PER_SIDE).map(x => x.r);
        const labeled = new Set(net ? ranked('net') : ranked('call').concat(ranked('put')));
        ctx.font = `10px ${P.font}`;
        ctx.textBaseline = 'middle';
        for (const r of rows) {
            const bh = Math.max(1, Math.min(GEX_BAR_MAX_H, r.h * GEX_BAR_FILL));
            const top = r.y - bh / 2;
            const hi = hover === r;
            const wantLabel = labelAll || labeled.has(r) || hi;
            if (net) {
                const len = lenOf(r.net);
                if (!len) continue;
                const pos = r.net >= 0;
                ctx.fillStyle = pos ? (hi ? fills.callHi : fills.call) : (hi ? fills.putHi : fills.put);
                ctx.fillRect(pos ? half : half - len, top, len, bh);
                P.drawn++;
                if (wantLabel && bh >= 6) _gexValue(ctx, c, r.net, pos ? half + len + 3 : half - len - 3, r.y, pos);
            } else {
                const cl = lenOf(r.call), pl = lenOf(r.put);
                if (cl) { ctx.fillStyle = hi ? fills.callHi : fills.call; ctx.fillRect(half, top, cl, bh); P.drawn++; }
                if (pl) { ctx.fillStyle = hi ? fills.putHi : fills.put; ctx.fillRect(half - pl, top, pl, bh); P.drawn++; }
                if (wantLabel && bh >= 6) {
                    if (cl && Math.abs(r.call) >= maxAbs * 0.08) _gexValue(ctx, c, r.call, half + cl + 3, r.y, true);
                    if (pl && Math.abs(r.put) >= maxAbs * 0.08) _gexValue(ctx, c, r.put, half - pl - 3, r.y, false);
                }
            }
        }

        // Center axis.
        ctx.fillStyle = c.borderStrong;
        ctx.fillRect(Math.round(half), GEX_HEAD_H, 1, H - GEX_HEAD_H);

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
        const spot = spotLevel(g);
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
        _gexHeader(ctx, W, c, net);
    }

    function _gexValue(ctx, c, v, x, y, right) {
        ctx.fillStyle = c.textMuted;
        ctx.textAlign = right ? 'left' : 'right';
        ctx.fillText(fmtGex(v), x, y);
    }

    function _gexHeader(ctx, W, c, net) {
        const half = W / 2;
        ctx.fillStyle = c.bgPanel;
        ctx.fillRect(0, 0, W, GEX_HEAD_H);
        ctx.fillStyle = c.border;
        ctx.fillRect(0, GEX_HEAD_H - 1, W, 1);
        ctx.font = `700 10px ${gexProfile.font}`;
        ctx.textBaseline = 'middle';
        ctx.textAlign = 'right';
        ctx.fillStyle = c.down;
        ctx.fillText(net ? '− NET' : 'PUT', half - 8, GEX_HEAD_H / 2);
        ctx.textAlign = 'left';
        ctx.fillStyle = c.up;
        ctx.fillText(net ? 'NET +' : 'CALL', half + 8, GEX_HEAD_H / 2);
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
        tip.replaceChildren(title,
            line('Call GEX', fmtGex(row.call)), line('Put GEX', fmtGex(row.put)), line('Net GEX', fmtGex(row.net)),
            line('Call OI', _intFmt.format(row.callOi ?? 0)), line('Put OI', _intFmt.format(row.putOi ?? 0)),
            line('Call Vol', _intFmt.format(row.callVol ?? 0)), line('Put Vol', _intFmt.format(row.putVol ?? 0)));
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
        P.view = saved === 'net' ? 'net' : 'split';
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
