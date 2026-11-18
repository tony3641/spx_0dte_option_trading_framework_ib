    // Charm heatmap for the SPX price chart: a Lightweight Charts series primitive painted behind the
    // candles. One column per frame (a minute) and one cell per strike; the value is the dealers' charm
    // exposure at that strike (see market/charm_history.py): positive = supportive (dealers buy as time
    // passes), negative = suppressive (they sell). Colors come from theme tokens through ``getColors``.
    // ======================================================================
    const CHARM_MIN_RATIO = 0.005;      // below this share of the scale a cell is left clear
    const CHARM_ALPHA_MIN = 0.06;
    const CHARM_ALPHA_SPAN = 0.34;
    const CHARM_ALPHA_STEPS = 24;       // alphas are quantized so the fill style changes rarely
    const CHARM_MAX_GAP_S = 300;        // a column never stretches over more than this much missing time
    const CHARM_STEP_S = 60;            // the width of one frame: the chart's bar interval

    // The color scale: the 95th percentile of |value| over every frame, so one extreme strike does not
    // wash out the rest. 0 when there is nothing to draw.
    function charmScaleMax(frames) {
        const abs = [];
        for (const f of frames) {
            for (const v of f.values) if (v) abs.push(Math.abs(v));
        }
        if (!abs.length) return 0;
        abs.sort((a, b) => a - b);
        return abs[Math.min(abs.length - 1, Math.floor(abs.length * 0.95))];
    }

    // null when the value is too small to show, else the sign and a quantized alpha in (0, 0.4].
    function charmCellStyle(value, maxAbs) {
        if (!value || !(maxAbs > 0)) return null;
        const ratio = Math.min(1, Math.abs(value) / maxAbs);
        if (ratio < CHARM_MIN_RATIO) return null;
        const step = Math.max(1, Math.round(ratio * CHARM_ALPHA_STEPS));
        return { positive: value > 0, alpha: CHARM_ALPHA_MIN + CHARM_ALPHA_SPAN * step / CHARM_ALPHA_STEPS };
    }

    class CharmHeatmapRenderer {
        constructor(source) { this._source = source; }
        draw(target) {
            target.useBitmapCoordinateSpace((scope) => {
                this._source.paint(scope.context, scope.horizontalPixelRatio, scope.verticalPixelRatio,
                                   scope.mediaSize.width);
            });
        }
    }

    class CharmHeatmapPaneView {
        constructor(source) { this._renderer = new CharmHeatmapRenderer(source); }
        zOrder() { return 'bottom'; }
        renderer() { return this._renderer; }
    }

    class CharmHeatmapPrimitive {
        constructor(getColors) {
            this._getColors = getColors;       // () => ({positive: '#rrggbb', negative: '#rrggbb'})
            this._frames = [];                 // [{time, strikes, values}] sorted by time
            this._maxAbs = 0;
            this._version = -1;
            this._endTime = null;              // the last time on the chart's time scale
            this._on = false;
            this._chart = null;
            this._series = null;
            this._requestUpdate = null;
            this._views = [new CharmHeatmapPaneView(this)];
            this.painted = 0;                  // cells filled by the last paint
        }

        attached(param) {
            this._chart = param.chart;
            this._series = param.series;
            this._requestUpdate = param.requestUpdate;
        }
        detached() { this._chart = this._series = this._requestUpdate = null; }
        paneViews() { return this._views; }
        updateAllViews() { /* the views read the source when they paint */ }

        // ``version`` changes whenever the frames do, so the color scale is recomputed only then.
        setFrames(frames, endTime, version) {
            this._frames = frames;
            this._endTime = endTime;
            if (version !== this._version) {
                this._version = version;
                this._maxAbs = charmScaleMax(frames);
            }
            this._redraw();
        }
        setVisible(on) {
            if (this._on === !!on) return;
            this._on = !!on;
            this._redraw();
        }
        refresh() { this._redraw(); }          // theme change: colors are read at paint time
        _redraw() { if (this._requestUpdate) this._requestUpdate(); }

        // x of a chart time: the time scale snaps a time to its nearest point; past the last point it
        // is extrapolated by whole bars.
        _xOf(ts, t, endX, spacing) {
            if (t > this._endTime) return endX + (t - this._endTime) / CHARM_STEP_S * spacing;
            return ts.timeToCoordinate(t);
        }

        paint(ctx, hr, vr, width) {
            this.painted = 0;
            const frames = this._frames;
            if (!this._on || !this._chart || !this._series || !frames.length || !(this._maxAbs > 0)
                || this._endTime === null) return;
            const ts = this._chart.timeScale();
            const endX = ts.timeToCoordinate(this._endTime);
            if (endX === null) return;
            const logical = ts.coordinateToLogical(endX);
            const next = logical === null ? null : ts.logicalToCoordinate(Math.round(logical) + 1);
            const spacing = next === null ? 0 : next - endX;
            if (!(spacing > 0)) return;
            const colors = this._getColors();
            const rgb = { true: hexToRgb(colors.positive), false: hexToRgb(colors.negative) };
            // Cell edges are rounded from the same coordinate on both sides of a boundary, so
            // neighbors share exact pixels: no gap, and no overlap that would apply the alpha twice.
            const half = spacing / 2;                       // a column is centered on its bar
            let fill = null;
            for (let i = 0; i < frames.length; i++) {
                const f = frames[i];
                const nextTime = i + 1 < frames.length ? frames[i + 1].time : f.time + CHARM_STEP_S;
                const c0 = this._xOf(ts, f.time, endX, spacing);
                const c1 = this._xOf(ts, Math.min(nextTime, f.time + CHARM_MAX_GAP_S), endX, spacing);
                if (c0 === null || c1 === null) continue;
                const x0 = c0 - half, x1 = c1 - half;
                if (x1 <= x0 || x1 < 0 || x0 > width) continue;
                const left = Math.round(x0 * hr);
                const w = Math.round(x1 * hr) - left;
                if (w <= 0) continue;
                const ks = f.strikes;
                for (let j = 0; j < ks.length; j++) {
                    const style = charmCellStyle(f.values[j], this._maxAbs);
                    if (!style) continue;
                    const lo = j === 0 ? ks[0] - (ks.length > 1 ? (ks[1] - ks[0]) / 2 : 0) : (ks[j - 1] + ks[j]) / 2;
                    const hi = j === ks.length - 1 ? ks[j] + (j > 0 ? (ks[j] - ks[j - 1]) / 2 : 0) : (ks[j] + ks[j + 1]) / 2;
                    const yHi = this._series.priceToCoordinate(hi);
                    const yLo = this._series.priceToCoordinate(lo);
                    if (yHi === null || yLo === null) continue;
                    const top = Math.round(yHi * vr);
                    const h = Math.round(yLo * vr) - top;
                    if (h <= 0) continue;
                    const color = `rgba(${rgb[style.positive].join(',')},${style.alpha.toFixed(3)})`;
                    if (color !== fill) { ctx.fillStyle = color; fill = color; }
                    ctx.fillRect(left, top, w, h);
                    this.painted++;
                }
            }
        }
    }
