#!/usr/bin/env python
"""Generate a comprehensive, self-contained HTML monthly trading report.

Combines the monthly performance report (daily PnL, weekly, risk metrics @ RF,
VIX regimes, SPX benchmark) with the strategy-level edge & risk analysis
(bull-put-credit-spread structure, per-leg win rates, stops & re-entry,
bootstrap significance, spread-capped tail stress, Monte Carlo, Kelly) and the
cross-month comparison.

``--monthly`` takes a QFX statement (full analysis) or an ``.xlsx`` trade-log workbook (day-level report: no
position direction, so the structure, stops/re-entry and Kelly sections show a notice; see
``report_inputs.py``).

Usage:
    python -m spx_trade_desk.tradelog.report.generate_report --monthly <july.qfx> \
        --ytd <ytd.qfx> --rf 0.04 --label "July 2026"

Output paths come from ``spx_trade_desk.resources`` (default
``reports/output/report.html``); the market-data cache is read from
``reports/data/``. Produces a single self-contained HTML file (embedded CSS +
inline SVG, no external dependencies, light/dark aware).
"""
from __future__ import annotations

import argparse
import html
import re
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

from spx_trade_desk.resources import REPORT_DATA_DIR, REPORT_OUTPUT_DIR
from spx_trade_desk.tradelog.report.report_inputs import load_report_inputs
from spx_trade_desk.tradelog.domain.risk_metrics import calculate_risk_metrics
from spx_trade_desk.tradelog.analysis import strategy_analysis as sa

DATA_DIR = REPORT_DATA_DIR

# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def esc(s):
    return html.escape(str(s))


def money(v, dec=2, sign=True):
    if v is None or (isinstance(v, float) and (np.isnan(v) or np.isinf(v))):
        return "—"
    s = f"{v:,.{dec}f}"
    if sign and v > 0:
        s = "+" + s
    return "$" + s


def pct(v, dec=2, sign=True):
    if v is None or (isinstance(v, float) and (np.isnan(v) or np.isinf(v))):
        return "—"
    s = f"{v * 100:.{dec}f}%"
    if sign and v > 0:
        s = "+" + s
    return s


def num(v, dec=2):
    if v is None or (isinstance(v, float) and (np.isnan(v) or np.isinf(v))):
        return "—"
    return f"{v:,.{dec}f}"


def _clean(v):
    """Convert a scalar to a plain JSON-safe Python type (None for NaN/NaT)."""
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        return float(v)
    if isinstance(v, (pd.Timestamp,)):
        return v.isoformat()
    from datetime import date as _date, datetime as _datetime
    if isinstance(v, (_date, _datetime)):
        return v.isoformat()
    return v


def _clean_dict(d: dict) -> dict:
    return {k: _clean(v) for k, v in d.items()}


# ---------------------------------------------------------------------------
# SVG generators (inline, class-based fills so light/dark CSS controls color)
# ---------------------------------------------------------------------------

def _y_ticks(ylo, yhi, n=5):
    return list(np.linspace(ylo, yhi, n))


def svg_equity(dates, cum, w=780, h=240, pad_l=66, pad_r=18, pad_t=18, pad_b=38):
    dates = [pd.Timestamp(d) for d in dates]
    cum = np.asarray(cum, dtype=float)
    n = len(cum)
    if n < 2:
        return ""
    span = float(cum.max() - cum.min())
    vpad = span * 0.10 if span > 0 else (abs(cum.max()) * 0.10 + 1.0)
    ylo, yhi = float(cum.min()) - vpad, float(cum.max()) + vpad

    def X(i):
        return pad_l + i / (n - 1) * (w - pad_l - pad_r)

    def Y(v):
        return pad_t + (yhi - v) / (yhi - ylo) * (h - pad_t - pad_b)

    out = [f'<svg viewBox="0 0 {w} {h}" class="chart" role="img" aria-label="Cumulative PnL equity curve">']
    # gridlines
    for t in _y_ticks(ylo, yhi):
        y = Y(t)
        out.append(f'<line class="grid" x1="{pad_l}" y1="{y:.1f}" x2="{w - pad_r}" y2="{y:.1f}"/>')
        out.append(f'<text class="muted" x="{pad_l - 8}" y="{y + 4:.1f}" text-anchor="end">{money(t, 0, False)}</text>')
    # x labels (4-5 dates)
    xidx = np.unique(np.linspace(0, n - 1, min(n, 5)).astype(int))
    for i in xidx:
        out.append(f'<text class="muted" x="{X(i):.1f}" y="{h - 14}" text-anchor="middle">{dates[i].strftime("%m-%d")}</text>')
    # area + line
    pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(cum))
    area = f"{X(0):.1f},{Y(ylo):.1f} {pts} {X(n - 1):.1f},{Y(ylo):.1f}"
    out.append(f'<polygon class="eq-area" points="{area}"/>')
    out.append(f'<polyline class="eq" points="{pts}"><title>final {money(cum[-1],0)}</title></polyline>')
    # end marker + label
    out.append(f'<circle class="eq-end" cx="{X(n - 1):.1f}" cy="{Y(cum[-1]):.1f}" r="3.5">'
               f'<title>{dates[-1].strftime("%Y-%m-%d")} {money(cum[-1],0)}</title></circle>')
    out.append(f'<text class="ink" x="{X(n - 1):.1f}" y="{Y(cum[-1]) - 8:.1f}" text-anchor="end" font-weight="600">{money(cum[-1], 0)}</text>')
    out.append("</svg>")
    return "\n".join(out)


def svg_pnl_bars(labels, values, w=780, h=240, pad_l=66, pad_r=18, pad_t=18,
                 pad_b=38, label_vals=False):
    values = [float(v) for v in values]
    n = len(values)
    if n == 0:
        return ""
    vmin, vmax = min(0.0, min(values)), max(0.0, max(values))
    span = vmax - vmin
    vpad = span * 0.10 if span > 0 else 1.0
    ylo, yhi = vmin - vpad, vmax + vpad

    def Y(v):
        return pad_t + (yhi - v) / (yhi - ylo) * (h - pad_t - pad_b)

    zero = Y(0.0)
    slot = (w - pad_l - pad_r) / n
    bw = slot * 0.60

    out = [f'<svg viewBox="0 0 {w} {h}" class="chart" role="img" aria-label="PnL bars">']
    for t in _y_ticks(ylo, yhi, 5):
        out.append(f'<line class="grid" x1="{pad_l}" y1="{Y(t):.1f}" x2="{w - pad_r}" y2="{Y(t):.1f}"/>')
        out.append(f'<text class="muted" x="{pad_l - 8}" y="{Y(t) + 4:.1f}" text-anchor="end">{money(t, 0, False)}</text>')
    for i, v in enumerate(values):
        x = pad_l + i * slot + (slot - bw) / 2
        y1, y2 = Y(v), zero
        if y1 > y2:
            y1, y2 = y2, y1
        cls = "gain" if v >= 0 else "loss"
        out.append(f'<rect class="{cls}" x="{x:.1f}" y="{y1:.1f}" width="{bw:.1f}" height="{max(1.5, y2 - y1):.1f}" rx="2">'
                   f'<title>{esc(labels[i])}: {money(v)}</title></rect>')
        if label_vals and n <= 8:
            out.append(f'<text class="ink" x="{x + bw / 2:.1f}" y="{Y(v) - 5 if v >= 0 else Y(v) + 13:.1f}" '
                       f'text-anchor="middle" font-size="11">{money(v, 0)}</text>')
    out.append(f'<line class="axis" x1="{pad_l}" y1="{zero:.1f}" x2="{w - pad_r}" y2="{zero:.1f}"/>')
    if n <= 22:
        step = max(1, int(np.ceil(n / 14)))
        for i in range(0, n, step):
            out.append(f'<text class="muted" x="{pad_l + i * slot + slot / 2:.1f}" y="{h - 14}" '
                       f'text-anchor="middle" font-size="10">{esc(labels[i])}</text>')
    out.append("</svg>")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# HTML component helpers
# ---------------------------------------------------------------------------

def table(headers, rows, col_classes=None, caption=None):
    h = "<tr>" + "".join(f"<th>{esc(x)}</th>" for x in headers) + "</tr>"
    body = []
    for r in rows:
        cells = []
        for j, c in enumerate(r):
            cls = col_classes[j] if col_classes and j < len(col_classes) else ""
            cells.append(f'<td class="{cls}">{c}</td>')
        body.append("<tr>" + "".join(cells) + "</tr>")
    cap = f"<caption>{esc(caption)}</caption>" if caption else ""
    return f'<table>{cap}{h}{"".join(body)}</table>'


def kpi_cards(items):
    cards = "".join(
        f'<div class="kpi"><div class="kpi-label">{esc(label)}</div>'
        f'<div class="kpi-value">{value}</div><div class="kpi-sub">{esc(sub)}</div></div>'
        for label, value, sub in items
    )
    return f'<div class="kpis">{cards}</div>'


def section(num, title, body):
    return (f'<section><div class="sec-head"><span class="sec-num">{num}</span>'
            f'<h2>{esc(title)}</h2></div>{body}</section>')


def callout(kind, title, text):
    return (f'<div class="callout {kind}"><div class="callout-title">{esc(title)}</div>'
            f'<div class="callout-body">{text}</div></div>')


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

CSS = """
:root {
  color-scheme: light;
  --surface:#fcfcfb; --ink:#0b0b0b; --muted:#898781; --grid:#e1e0d9;
  --baseline:#c3c2b7; --blue:#2a78d6; --gain:#1baf7a; --loss:#e34948;
  --card:#f9f9f7; --line:rgba(11,11,11,0.10);
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    color-scheme: dark;
    --surface:#1a1a19; --ink:#ffffff; --muted:#898781; --grid:#2c2c2a;
    --baseline:#383835; --blue:#3987e5; --gain:#199e70; --loss:#e66767;
    --card:#0d0d0d; --line:rgba(255,255,255,0.10);
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --surface:#1a1a19; --ink:#ffffff; --muted:#898781; --grid:#2c2c2a;
  --baseline:#383835; --blue:#3987e5; --gain:#199e70; --loss:#e66767;
  --card:#0d0d0d; --line:rgba(255,255,255,0.10);
}

body { margin:0; font-family: system-ui,-apple-system,"Segoe UI",sans-serif;
       background:var(--card); color:var(--ink); line-height:1.5; }
.viz { max-width: 980px; margin: 0 auto; padding: 40px 28px 80px;
       background: var(--surface); color: var(--ink); }
h1 { font-size: 26px; margin: 0 0 4px; letter-spacing:-0.01em; }
h2 { font-size: 17px; margin: 0; }
p { font-size: 14px; color: var(--ink); }
.meta { color: var(--muted); font-size: 13px; margin-bottom: 22px; }
section { margin: 40px 0; padding-top: 8px; }
.sec-head { display:flex; align-items:center; gap:10px; border-bottom:1px solid var(--line);
            padding-bottom:10px; margin-bottom:18px; }
.sec-num { width:24px; height:24px; border-radius:50%; background:var(--blue); color:#fff;
           display:flex; align-items:center; justify-content:center; font-size:13px; font-weight:600; }
.kpis { display:grid; grid-template-columns: repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin:22px 0; }
.kpi { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; }
.kpi-label { font-size:12px; color:var(--muted); }
.kpi-value { font-size:22px; font-weight:700; margin-top:2px; font-variant-numeric: tabular-nums; }
.kpi-sub { font-size:11px; color:var(--muted); margin-top:2px; }
table { border-collapse: collapse; width:100%; font-size:13px; margin:10px 0 4px; }
caption { text-align:left; color:var(--muted); font-size:12px; margin-bottom:6px; }
th { text-align:right; padding:7px 10px; border-bottom:1px solid var(--baseline);
     color:var(--muted); font-weight:600; white-space:nowrap; }
th:first-child, td:first-child { text-align:left; }
td { padding:6px 10px; border-bottom:1px solid var(--line); font-variant-numeric: tabular-nums; }
tr:hover td { background: var(--card); }
.pos { color: var(--gain); } .neg { color: var(--loss); }
.muted { color: var(--muted); }
.chart { width:100%; height:auto; margin: 8px 0 2px; }
svg .eq { fill:none; stroke:var(--blue); stroke-width:2; }
svg .eq-area { fill:var(--blue); opacity:0.12; stroke:none; }
svg .eq-end { fill:var(--blue); stroke:var(--surface); stroke-width:1.5; }
svg .gain { fill:var(--gain); }
svg .loss { fill:var(--loss); }
svg .grid { stroke:var(--grid); stroke-width:1; }
svg .axis { stroke:var(--baseline); stroke-width:1; }
svg text { font-family: system-ui,-apple-system,"Segoe UI",sans-serif; }
svg .ink { fill:var(--ink); font-size:11px; }
svg .muted { fill:var(--muted); font-size:10px; }
.callout { border-radius:10px; padding:14px 16px; margin:14px 0; border:1px solid var(--line); }
.callout-title { font-weight:600; font-size:13px; margin-bottom:6px; }
.callout.warn { background:rgba(234,163,0,0.10); border-color:rgba(234,163,0,0.45); }
.callout.info { background:rgba(42,120,214,0.08); border-color:rgba(42,120,214,0.35); }
.callout.serious { background:rgba(211,59,59,0.10); border-color:rgba(211,59,59,0.40); }
.callout-body { font-size:13px; }
ul { font-size:14px; margin:8px 0; padding-left:20px; }
li { margin:6px 0; }
.chart-grid { display:grid; gap:8px; }
.foot { margin-top:40px; color:var(--muted); font-size:11px; border-top:1px solid var(--line); padding-top:14px; }
"""


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

def compute_metrics(daily, capital, rf, spx, vix):
    return calculate_risk_metrics(daily, capital, rf, spx, vix)


def needs_qfx_notice(what):
    """Notice that replaces a section a date-only workbook cannot support."""
    return callout(
        "info", "Needs QFX timestamps",
        f"This section needs {what}, which a trade-log workbook does not carry (it has dates, not "
        "execution times). Load a QFX statement for the same period to see it.")


def provenance_box(source):
    """The 'About this report' box shown on workbook reports."""
    capital = ("assumed, the workbook has no balances; pass initial_capital to change it"
               if source["capital_assumed"] else "supplied")
    items = [
        "Source: " + source["description"],
        "Accounts: " + ", ".join(source["accounts"]),
        "Scope: " + source["scope"],
        f"Capital at the start of the workbook: {money(source['capital_at_start'], 0, False)} ({capital})",
        f"Coverage: {source['contract_days']} contract-days, {source['closed_intraday']} closed within the "
        "same day (their direction cannot be recovered from dates)",
    ]
    return callout("info", "About this report",
                   "<ul>" + "".join(f"<li>{esc(x)}</li>" for x in items) + "</ul>")


def _pooled_paragraph(day_pnl, pooled_pnl, pool_window, pool_ci, pool_p, conclude=False):
    """Pooled-significance paragraph for section 8.

    The QFX report keeps its original fixed closing sentence (``conclude=False``, byte-for-byte unchanged).
    A workbook report passes ``conclude=True`` so the sentence follows the pooled numbers: earlier months are
    pooled in automatically there, so the pooled result is often neither positive nor significant.
    """
    if not conclude or (pool_p < 0.05 and pool_ci[1] > 0):
        verdict = (f"the edge is statistically significant when measured across the full window, "
                   f"not on one month's {len(day_pnl)} days alone.")
    elif pool_ci[1] > 0:
        verdict = "positive, but not statistically significant even when measured across the full window."
    else:
        verdict = "negative on average across the full window, so no edge is demonstrated."
    return (
        f"<p class='muted' style='font-size:13px'>Pooled across {len(pooled_pnl)} days "
        f"({pool_window}): day EV <b>{money(pool_ci[1])}</b>, "
        f"95% CI [{money(pool_ci[0])}, {money(pool_ci[2])}], <b>p={pool_p:.3f}</b> — "
        f"{verdict}</p>")


def _position_analytics(positions, spreads, daily, spx, gaps):
    """Spread/leg statistics, stops & re-entry, gap stress and Kelly.

    Moved verbatim out of ``build_report``. Every figure here needs position direction, so a source without
    it (the trade-log workbook) uses ``_no_position_analytics`` instead.
    """
    # ---- spread / leg stats ----
    leg_stats = sa.per_leg_stats(positions)
    short_row = leg_stats[leg_stats["leg"] == "short"].iloc[0] if len(leg_stats) else None
    long_row = leg_stats[leg_stats["leg"] == "long"].iloc[0] if len(leg_stats) else None
    n_paired = int(spreads["paired"].sum()) if len(spreads) else 0
    n_short = len(spreads)
    unpaired = spreads[~spreads["paired"]] if len(spreads) else pd.DataFrame()
    widths = pd.to_numeric(spreads["width"], errors="coerce").dropna() if len(spreads) else pd.Series(dtype=float)
    credits = pd.to_numeric(spreads["credit_ct"], errors="coerce").dropna() if len(spreads) else pd.Series(dtype=float)
    maxloss = pd.to_numeric(spreads["max_loss_ct"], errors="coerce").dropna() if len(spreads) else pd.Series(dtype=float)
    spread_ev = (short_row["ev"] + long_row["ev"]) if (short_row is not None and long_row is not None) else np.nan

    # ---- stops & re-entry (short-leg loss >= 3.5x credit ~= 6x spread price) ----
    stops = sa.stop_events(positions, stop_multiple=3.5)
    stop_rows = stops[stops["is_stop"]]
    stop_days = set(pd.Timestamp(d).date() for d in stop_rows["date"]) if len(stop_rows) else set()
    reentry = sa.reentry_test(positions, stop_rows) if len(stop_rows) else pd.DataFrame()
    dd2 = daily.copy()
    dd2["d"] = pd.to_datetime(dd2["activity_date"]).dt.date
    stop_day_pnl = dd2[dd2["d"].isin(stop_days)]["realized_pnl"] if stop_days else pd.Series(dtype=float)
    normal_day_pnl = dd2[~dd2["d"].isin(stop_days)]["realized_pnl"] if stop_days else dd2["realized_pnl"]
    positions_per_day = len(positions) / max(1, len(daily))
    day_ev_pred = positions_per_day * positions["total_pnl"].mean() if len(positions) else np.nan

    # ---- tail ----
    spx_map = {}
    try:
        spx_tmp = spx.copy()
        spx_tmp["activity_date"] = pd.to_datetime(spx_tmp["activity_date"]).dt.date
        spx_map = dict(zip(spx_tmp["activity_date"], pd.to_numeric(spx_tmp["spx_close"], errors="coerce")))
    except Exception:
        spx_map = {}
    stress = sa.gap_stress(spreads, spx_map, gaps) if len(spreads) else pd.DataFrame()

    # ---- Kelly ----
    if short_row is not None and not np.isnan(short_row["avg_win"]) and not np.isnan(short_row["avg_loss"]):
        f_kelly, half = sa.kelly_binary(short_row["win_rate"], short_row["avg_win"] / short_row["avg_loss"])
    else:
        f_kelly, half = np.nan, np.nan
    return SimpleNamespace(
        short_row=short_row, long_row=long_row, n_paired=n_paired, n_short=n_short, unpaired=unpaired,
        widths=widths, credits=credits, maxloss=maxloss, spread_ev=spread_ev,
        stop_rows=stop_rows, reentry=reentry, stop_day_pnl=stop_day_pnl, normal_day_pnl=normal_day_pnl,
        positions_per_day=positions_per_day, day_ev_pred=day_ev_pred,
        stress=stress, f_kelly=f_kelly, half=half)


def _no_position_analytics():
    """Neutral stand-ins for a source without position direction; the guarded sections never read them."""
    empty = pd.DataFrame()
    return SimpleNamespace(
        short_row=None, long_row=None, n_paired=0, n_short=0, unpaired=empty,
        widths=pd.Series(dtype=float), credits=pd.Series(dtype=float), maxloss=pd.Series(dtype=float),
        spread_ev=np.nan, stop_rows=empty, reentry=empty,
        stop_day_pnl=pd.Series(dtype=float), normal_day_pnl=pd.Series(dtype=float),
        positions_per_day=np.nan, day_ev_pred=np.nan, stress=empty, f_kelly=np.nan, half=np.nan)


def build_report(args) -> tuple[str, dict]:
    label = args.label
    rf = args.rf

    # ---- load the source (QFX statement or trade-log workbook) ----
    inputs = load_report_inputs(args)
    positional = inputs.has_positions
    full_daily = inputs.daily
    daily = full_daily
    positions = inputs.positions if positional else pd.DataFrame()
    spreads = inputs.spreads if positional else pd.DataFrame()
    prior_daily = inputs.prior_daily
    prior_capital = inputs.prior_capital

    # ---- optional month filter (slice one calendar month out of the file) ----
    if args.month:
        m_start = pd.Timestamp(args.month + "-01")
        m_end = m_start + pd.offsets.MonthEnd(0)
        daily = daily[(pd.to_datetime(daily["activity_date"]) >= m_start) &
                      (pd.to_datetime(daily["activity_date"]) <= m_end)]
        if positional:
            positions = positions[(pd.to_datetime(positions["date"]) >= m_start) &
                                  (pd.to_datetime(positions["date"]) <= m_end)]
            spreads = spreads[(pd.to_datetime(spreads["date"]) >= m_start) &
                              (pd.to_datetime(spreads["date"]) <= m_end)]
        # running capital: full-file starting balance + cumulative PnL through prior month
        prior = full_daily[pd.to_datetime(full_daily["activity_date"]).dt.date < m_start.date()]
        initial_capital = inputs.initial_capital + float(prior["realized_pnl"].sum())
        if inputs.prior_from_window:
            # a workbook carries its own earlier months: they feed the cross-month context
            prior_daily = prior if len(prior) else None
            prior_capital = inputs.initial_capital
    else:
        initial_capital = inputs.initial_capital

    if not daily.empty:
        month_start = pd.Timestamp(daily["activity_date"].min())
        month_end = pd.Timestamp(daily["activity_date"].max())
        month_str = month_start.strftime("%Y-%m")
        window_label = month_str
        span_text = month_start.strftime("%B %Y")
        if inputs.source is not None and not args.month and month_end.strftime("%Y-%m") != month_str:
            # a workbook window with no month filter can span several months
            window_label = f"{month_str} → {month_end.strftime('%Y-%m')}"
            span_text = f"{month_start.strftime('%B %Y')} – {month_end.strftime('%B %Y')}"
    else:
        return "<p>No trades found in the monthly file for the selected window.</p>", None

    # ---- market data ----
    spx, vix = sa.load_market_data(str(DATA_DIR / "spx_closes.csv"),
                                   str(DATA_DIR / "vix_closes.csv"),
                                   month_start.year, month_end,
                                   offline=getattr(args, "offline", False))

    # ---- risk metrics @ RF ----
    m = compute_metrics(daily, initial_capital, rf, spx, vix)
    total_pnl = float(daily["realized_pnl"].sum())
    n_trades = int(daily["trade_count"].sum()) if "trade_count" in daily else 0
    n_contracts = int(daily["option_contracts_traded"].sum())
    pos_days = int(m.get("positive_cycles") or 0)
    neg_days = int(m.get("negative_cycles") or 0)
    day_ev = total_pnl / max(1, len(daily))

    # ---- day bootstrap ----
    day_pnl = daily["realized_pnl"].astype(float).values
    ci = sa.bootstrap_ci(day_pnl)
    p_val = sa.bootstrap_pval_gt0(day_pnl)

    # ---- weekly ----
    dd = daily.copy()
    dd["dt"] = pd.to_datetime(dd["activity_date"])
    dd["week"] = dd["dt"].dt.strftime("%G-W%V")
    weekly = dd.groupby("week").agg(
        pnl=("realized_pnl", "sum"),
        commission=("commission_spent", "sum"),
        contracts=("option_contracts_traded", "sum"),
        start=("activity_date", "min"),
    ).reset_index()

    # ---- cross-month ----
    cross_rows = []
    cross_order = []
    if prior_daily is not None:
        ytd_daily = prior_daily.copy()
        ytd_daily["month"] = pd.to_datetime(ytd_daily["activity_date"]).dt.to_period("M").astype(str)
        running = prior_capital
        prior_end = None
        for mm in sorted(ytd_daily["month"].unique()):
            if mm >= month_str:
                continue  # target month comes from --monthly
            mv = ytd_daily[ytd_daily["month"] == mm]
            if mv.empty:
                continue
            mm_metrics = calculate_risk_metrics(mv, running, rf)
            mm_pnl = float(mv["realized_pnl"].sum())
            cross_rows.append({
                "month": mm,
                "pnl": mm_pnl,
                "ret": mm_pnl / running,
                "sharpe": mm_metrics.get("sharpe"),
                "sortino": mm_metrics.get("sortino"),
                "ev": mm_metrics.get("net_ev"),
                "win": mm_metrics.get("positive_cycles", 0) / max(1, len(mv)),
                "max_loss": mm_metrics.get("max_loss"),
                "days": len(mv),
            })
            cross_order.append(mm)
            running += mm_pnl
    # target month row
    cross_rows.append({
        "month": window_label,
        "pnl": total_pnl,
        "ret": total_pnl / initial_capital,
        "sharpe": m.get("sharpe"),
        "sortino": m.get("sortino"),
        "ev": m.get("net_ev"),
        "win": pos_days / max(1, len(daily)),
        "max_loss": m.get("max_loss"),
        "days": len(daily),
    })
    cross_order.append(window_label)

    # ---- pooled day-level bootstrap (prior months + this month) ----
    pooled_pnl = day_pnl
    pool_ci = ci
    pool_p = p_val
    pool_window = "this month"
    if prior_daily is not None:
        prior = prior_daily[pd.to_datetime(prior_daily["activity_date"]).dt.date < month_start.date()]
        if len(prior):
            pooled_pnl = np.concatenate([prior["realized_pnl"].astype(float).values, day_pnl])
            pool_ci = sa.bootstrap_ci(pooled_pnl)
            pool_p = sa.bootstrap_pval_gt0(pooled_pnl)
            pool_window = (f"{pd.Timestamp(prior['activity_date'].min()).strftime('%Y-%m')} "
                           f"→ {month_start.strftime('%Y-%m')}")

    # ---- spread / leg stats, stops, tail stress, Kelly (all need position direction) ----
    gaps = [0.005, 0.01, 0.02, 0.03, 0.04]
    pa = (_position_analytics(positions, spreads, daily, spx, gaps) if positional
          else _no_position_analytics())
    short_row, long_row = pa.short_row, pa.long_row
    n_paired, n_short, unpaired = pa.n_paired, pa.n_short, pa.unpaired
    widths, credits, maxloss, spread_ev = pa.widths, pa.credits, pa.maxloss, pa.spread_ev
    stop_rows, reentry = pa.stop_rows, pa.reentry
    stop_day_pnl, normal_day_pnl = pa.stop_day_pnl, pa.normal_day_pnl
    positions_per_day, day_ev_pred = pa.positions_per_day, pa.day_ev_pred
    stress, f_kelly, half = pa.stress, pa.f_kelly, pa.half
    mc = sa.monte_carlo_days(day_pnl)
    worst_day = float(day_pnl.min())

    # =======================================================================
    # Assemble HTML
    # =======================================================================
    P = []  # parts

    # ---- 1. Executive summary ----
    kpis = [
        ("Net Realized PnL", money(total_pnl), f"{pct(total_pnl / initial_capital)} on {money(initial_capital,0,False)}"),
        ("Daily EV", money(day_ev), f"across {len(daily)} trading days"),
        ("Sharpe @ {:.0f}% RF".format(rf * 100), num(m.get("sharpe"), 2), f"Sortino {num(m.get('sortino'), 2)}"),
        ("Winning days", f"{pos_days}/{len(daily)}", f"{pct(pos_days / max(1, len(daily)), 1, False)} green"),
        ("Best / worst day", money(m.get("max_gain")), f"worst {money(m.get('max_loss'))}"),
        ("vs SPX", pct(m.get("return_delta_vs_spx")), f"SPX {pct(m.get('spx_period_return'), 2, False)}"),
    ]
    P.append(section("1", "Executive Summary", kpi_cards(kpis)))

    # ---- 2. Daily PnL ----
    eq_svg = svg_equity(dd["activity_date"], dd["realized_pnl"].cumsum())
    bars_svg = svg_pnl_bars([pd.Timestamp(d).strftime("%m-%d") for d in dd["activity_date"]],
                            dd["realized_pnl"])
    daily_rows = [[pd.Timestamp(r["activity_date"]).strftime("%Y-%m-%d"),
                   f'<span class="{"pos" if r["realized_pnl"] >= 0 else "neg"}">{money(r["realized_pnl"])}</span>',
                   int(r["option_contracts_traded"]), int(r["trade_count"]),
                   money(r["cumulative_pnl"])]
                  for _, r in daily.iterrows()]
    # notable positions (top winners / losers)
    if len(positions):
        notable = positions.sort_values("total_pnl")
        bottom = notable.head(5).iloc[::-1]
        top = notable.tail(5).iloc[::-1]
        notable_rows = []
        for _, r in pd.concat([bottom, top]).iterrows():
            notable_rows.append([
                pd.Timestamp(r["date"]).strftime("%m-%d") if pd.notna(r["date"]) else "—",
                f"P{r['strike']:.0f}",
                "Sell" if r["direction"] == "short" else "Buy",
                int(r["contracts"]),
                f'<span class="{"pos" if r["total_pnl"] >= 0 else "neg"}">{money(r["total_pnl"])}</span>',
            ])
        notable_html = "<h3 style='font-size:14px;margin:18px 0 6px'>Notable positions (5 worst / 5 best)</h3>" + \
            table(["Date", "Strike", "Leg", "Contracts", "PnL"], notable_rows)
    else:
        notable_html = ""
    P.append(section("2", "Daily PnL & Equity Curve",
        '<div class="chart-grid">' + eq_svg + bars_svg + "</div>" +
        table(["Date", "Realized PnL", "Contracts", "Trades", "Cumulative"], daily_rows) +
        notable_html))

    # ---- 3. Weekly ----
    week_rows = [[pd.Timestamp(w["start"]).strftime("%Y-%m-%d"), money(w["pnl"]),
                  int(w["contracts"]), money(w["commission"])]
                 for _, w in weekly.iterrows()]
    P.append(section("3", "Weekly Breakdown",
        svg_pnl_bars([str(w["start"])[:10] for _, w in weekly.iterrows()], weekly["pnl"],
                     label_vals=True) +
        table(["Week start", "Week PnL", "Contracts", "Commission"], week_rows)))

    # ---- 4. Risk-adjusted @ RF ----
    risk_rows = [
        ["Sharpe ratio @ {:.0f}% RF".format(rf * 100), num(m.get("sharpe"), 2)],
        ["Sortino ratio", num(m.get("sortino"), 2)],
        ["Daily volatility (of return)", pct(m.get("std_daily"), 3, False)],
        ["Daily EV (net)", money(m.get("net_ev"))],
        ["Commission drag", pct(m.get("commission_drag"), 1, False)],
        ["Max single-day gain / loss", f"{money(m.get('max_gain'))} / {money(m.get('max_loss'))}"],
        ["Longest recovery from drawdown", f"{int(m.get('max_recovery_days'))} days" if m.get("max_recovery_days") else "—"],
    ]
    P.append(section("4", "Risk-Adjusted Performance (RF {:.0f}%)".format(rf * 100),
        table(["Metric", "Value"], risk_rows)))

    # ---- 5. VIX regimes ----
    regime_tbl = m.get("vix_regime_table")
    regime_body = ""
    if regime_tbl is not None and not regime_tbl.empty:
        labels = regime_tbl["regime"].tolist()
        vals = regime_tbl["net_pnl"].fillna(0).tolist()
        regime_body = svg_pnl_bars([l.split("(")[0] for l in labels], vals, label_vals=True)
        regime_body += table(
            ["Regime", "Days", "Net PnL", "Win rate", "Avg VIX close", "Net EV/day"],
            [[r["regime"], int(r["days"]), money(r["net_pnl"]),
              pct(r["win_rate"], 1, False) if pd.notna(r["win_rate"]) else "—",
              num(r["avg_vix_close"], 1) if pd.notna(r["avg_vix_close"]) else "—",
              money(r["net_ev"]) if pd.notna(r["net_ev"]) else "—"]
             for _, r in regime_tbl.iterrows()])
    P.append(section("5", "VIX Regime Analysis", regime_body or "<p>No VIX overlap.</p>"))

    # ---- 6. SPX benchmark ----
    spx_rows = [
        ["SPX period return", pct(m.get("spx_period_return"), 2, False)],
        ["Strategy vs SPX (return delta)", pct(m.get("return_delta_vs_spx"))],
        ["SPX correlation", num(m.get("spx_corr"), 2)],
        ["SPX beta", num(m.get("spx_beta"), 3)],
        ["Annualized alpha vs SPX", pct(m.get("spx_alpha"), 1)],
        ["Overlap days", int(m.get("spx_overlap_days")) if m.get("spx_overlap_days") else "—"],
    ]
    P.append(section("6", "SPX Benchmark & Market Context",
        table(["Metric", "Value"], spx_rows)))

    # ---- 7. Strategy structure ----
    if short_row is not None:
        leg_rows = [
            ["Short leg (the bet)", f"{int(short_row['n'])}",
             pct(short_row["win_rate"], 1, False), money(short_row["avg_win"]),
             money(short_row["avg_loss"]), money(short_row["ev"])],
            ["Long leg (insurance)", f"{int(long_row['n'])}",
             pct(long_row["win_rate"], 1, False), money(long_row["avg_win"]),
             money(long_row["avg_loss"]), money(long_row["ev"])],
        ]
        structure = table(["Leg", "Positions", "Win rate", "Avg win", "Avg loss", "EV/position"],
                          leg_rows)
        structure += "<p class='muted' style='font-size:13px'>" + \
            f"Spread geometry: width median <b>{widths.median():.0f} pts</b>, net credit <b>${credits.median():.2f}</b>/contract, " + \
            f"max loss <b>${maxloss.median():,.0f}</b>/contract. Paired shorts: <b>{n_paired}/{n_short}</b> " + \
            f"({pct(n_paired / max(1, n_short), 1, False)}). Net EV per spread pair: <b>{money(spread_ev)}</b>.</p>"
        if len(unpaired):
            u = unpaired.iloc[0]
            structure += callout("warn", "Unpaired short to verify",
                f"{u['date']}: short P{u['short_strike']:.0f} x {u['qty']} did not pair to a same-day "
                f"lower-strike long — likely a close-matching artifact, but worth a manual check.")
        P.append(section("7", "Strategy Structure — Bull Put Credit Spreads", structure))
    elif not positional:
        P.append(section("7", "Strategy Structure — Bull Put Credit Spreads",
                         needs_qfx_notice("position direction (which leg of each spread was opened first)")))

    # ---- 8. Edge & significance ----
    if not positional:
        edge_text = (
            f"<p>Day-level EV for {label or 'this window'} is <b>{money(ci[1])}</b>/day with 95% CI "
            f"[{money(ci[0])}, {money(ci[2])}] and <b>p={p_val:.3f}</b> "
            f"(H0: mean ≤ 0; {len(day_pnl)} days).</p>")
        if len(pooled_pnl) > len(day_pnl):
            edge_text += _pooled_paragraph(day_pnl, pooled_pnl, pool_window, pool_ci, pool_p, conclude=True)
    elif short_row is not None and not np.isnan(short_row["avg_loss"]):
        be = sa.breakeven_win_rate(short_row["avg_win"], short_row["avg_loss"])
        margin = short_row["win_rate"] - be
        edge_text = (
            f"<p>Day-level EV for {label or 'this month'} is <b>{money(ci[1])}</b>/day with 95% CI "
            f"[{money(ci[0])}, {money(ci[2])}] and <b>p={p_val:.3f}</b> "
            f"(H0: mean ≤ 0; {len(day_pnl)} days). The short leg wins <b>{pct(short_row['win_rate'],1,False)}</b> vs a breakeven "
            f"of <b>{pct(be,1,False)}</b> — a <b>{pct(margin,1,False)}</b> edge margin.</p>")
        if len(pooled_pnl) > len(day_pnl):
            edge_text += _pooled_paragraph(day_pnl, pooled_pnl, pool_window, pool_ci, pool_p)
    else:
        edge_text = "<p>Insufficient position data.</p>"
    cross_headers = ["Month", "PnL", "Return", "Sharpe@RF", "Sortino", "EV/day", "Win days", "Max loss"]
    cross_rows_html = []
    for mm in cross_order:
        r = next(x for x in cross_rows if x["month"] == mm)
        cross_rows_html.append([
            r["month"], money(r["pnl"]), pct(r["ret"]),
            num(r["sharpe"], 2) if pd.notna(r["sharpe"]) else "—",
            num(r["sortino"], 2) if pd.notna(r["sortino"]) else "—",
            money(r["ev"]) if pd.notna(r["ev"]) else "—",
            pct(r["win"], 1, False), money(r["max_loss"]) if pd.notna(r["max_loss"]) else "—"])
    P.append(section("8", "Edge & Statistical Significance",
        edge_text + "<h3 style='font-size:14px;margin:14px 0 6px'>Cross-month context</h3>" +
        table(cross_headers, cross_rows_html)))

    # ---- 9. Stops & re-entry ----
    if positional:
        stop_text = ""
        if len(stop_rows):
            re_rows = [[str(r["stop_date"])[:10], money(r["stop_loss"]), int(r["n_after"]),
                        money(r["pnl_after"])] for _, r in reentry.iterrows()]
            stop_text += table(["Stop date", "Stop loss", "Trades after", "PnL after"], re_rows)
            stop_text += "<p class='muted' style='font-size:13px'>" + \
                f"Stop days average <b>{money(stop_day_pnl.mean())}</b> vs non-stop days <b>{money(normal_day_pnl.mean())}</b>. " + \
                f"Trades-after-stops net <b>{money(reentry['pnl_after'].sum())}</b>. " + \
                "Re-entry recovers on reversal days and amplifies losses on continuation days.</p>"
        else:
            stop_text += "<p>No stop-zone events this month (short-leg loss ≥ 3.5× credit, ≈ 6× spread price).</p>"
        stop_text += callout("info", "Day-edge vs trade-edge",
            f"positions/day ({positions_per_day:.1f}) × per-position EV ({money(positions['total_pnl'].mean())}) = "
            f"<b>{money(day_ev_pred)}</b> vs actual day EV <b>{money(day_ev)}</b>. The residual "
            f"({money(day_ev - day_ev_pred)})/day is non-option income (dividends) in the daily total — the "
            f"tradeable edge is the sum of the position edges; there is no hidden re-entry alpha beyond the trades themselves.")
    else:
        stop_text = needs_qfx_notice("position direction and execution order (which fills opened and which closed)")
    P.append(section("9", "Stops, Re-Entry & Day-vs-Trade Edge", stop_text))

    # ---- 10. Tail ----
    stress_text = ""
    if len(stress):
        srows = []
        for g in gaps:
            sub = stress[stress["gap"] == g]
            if sub.empty:
                continue
            srows.append([f"−{g*100:.1f}%", money(sub["pnl"].median()), money(np.percentile(sub["pnl"], 90)),
                          money(sub["pnl"].min())])
        stress_text = table(["SPX closes", "Median day", "p90 day", "Worst day"], srows)
        stress_text += "<p class='muted' style='font-size:13px'>Gross book loss if one day's spreads are held to expiry " + \
            "(capped at spread width). Realized losses are far smaller because stops + far-OTM strikes cut early.</p>"
    mc_text = (
        f"<p>Simulated months (resampling {len(day_pnl)} actual days): mean <b>{money(mc.get('month_mean'))}</b>, "
        f"p5 <b>{money(mc.get('month_p5'))}</b>, <b>{pct(mc.get('month_p_losing'),1,False)}</b> chance a month loses. "
        f"Tail days in this history: <b>{mc.get('days_per_year_neg1000'):.1f}</b>/yr worse than −$1,000, "
        f"<b>{mc.get('days_per_year_neg1500'):.1f}</b>/yr worse than −$1,500; worst observed day <b>{money(mc.get('worst_day'))}</b>.</p>"
    )
    if mc.get("worst_day", 0) < -1000:
        mc_text += callout("serious", "The one-day tail",
            f"Your worst day was <b>{money(mc.get('worst_day'))}</b>. A hard daily stop-loss of <b>−$1,000</b> "
            f"would have capped it and every day worse than −$1k this month.")
    else:
        mc_text += callout("info", "No day worse than −$1,000 this month",
            f"Your worst single day was <b>{money(mc.get('worst_day'))}</b> — no session exceeded −$1,000. "
            f"The month's losses were kept inside the stop zone, so no daily-loss cap was needed in practice.")
    P.append(section("10", "Tail Risk — “Works Until It Doesn’t”", stress_text + mc_text))

    # ---- 11. Sizing & Kelly ----
    if positional:
        pnl2 = "-2% close"
        sizing_rows = [
            ["Binary Kelly (short leg, per position)", pct(f_kelly, 2, False) if not np.isnan(f_kelly) else "—"],
            ["Half-Kelly", pct(half, 2, False) if not np.isnan(half) else "—"],
            ["Contracts traded", str(n_contracts)],
            ["Avg contracts/day", f"{n_contracts / max(1, len(daily)):.1f}"],
            ["Worst single −2% day (gross)", money(stress[stress['gap'] == 0.02]['pnl'].min()) if len(stress) else "—"],
        ]
        P.append(section("11", "Position Sizing & Kelly",
            table(["Metric", "Value"], sizing_rows) +
            callout("warn", "Sizing vs the tail",
                f"One −2% SPX close with the book on is worth <b>{money(stress[stress['gap'] == 0.02]['pnl'].median())}</b> "
                f"(median) on <b>{money(initial_capital, 0, False)}</b> of capital — more than the account. "
                f"The spread caps it, but the cap is large relative to the edge; sizing must be set so a −2% day "
                f"is survivable.")))
    else:
        P.append(section("11", "Position Sizing & Kelly",
            needs_qfx_notice("the short-leg win rate and reconstructed spreads (position direction)")))

    # ---- 12. Takeaways (conditional on the month's gain/loss) ----
    month_positive = total_pnl > 0
    losing_days = int((day_pnl < 0).sum())
    capped_total = float(np.where(day_pnl < -1000.0, -1000.0, day_pnl).sum())
    month_flips_positive = capped_total > 0
    worst_day_val = float(mc.get("worst_day") or 0.0)
    has_pooled = len(pooled_pnl) > len(day_pnl)

    t = []

    # 1. Month verdict
    if month_positive:
        t.append(f"<b>Positive month:</b> {money(total_pnl)} ({pct(total_pnl / initial_capital)}) with "
                 f"{pos_days}/{len(daily)} days green.")
    else:
        verdict = (f"<b>Losing month:</b> {money(total_pnl)} ({pct(total_pnl / initial_capital)}) with only "
                   f"{pos_days}/{len(daily)} days green ({losing_days} red).")
        if worst_day_val < -1000 and month_flips_positive:
            verdict += (f" The loss is concentrated — the worst day ({money(worst_day_val)}) alone exceeds it; "
                        f"a −$1,000 daily cap would have flipped the month to ≈ <b>{money(capped_total)}</b>.")
        t.append(verdict)

    # 2. Edge statement
    if day_ev > 0:
        if p_val < 0.05:
            edge = (f"<b>Positive expectancy:</b> day EV {money(day_ev)} (95% CI [{money(ci[0])}, {money(ci[2])}], "
                    f"p={p_val:.3f}) — positive and statistically significant.")
        else:
            edge = (f"<b>Positive expectancy:</b> day EV {money(day_ev)} (95% CI [{money(ci[0])}, {money(ci[2])}], "
                    f"p={p_val:.3f}) — positive, but {len(day_pnl)} days aren't enough to prove it on their own.")
            if has_pooled:
                if pool_p < 0.05:
                    edge += (f" Pooled over {len(pooled_pnl)} days ({pool_window}) it is significant "
                             f"(EV {money(pool_ci[1])}, p={pool_p:.3f}).")
                else:
                    edge += (f" Pooled over {len(pooled_pnl)} days ({pool_window}) it is also not "
                             f"significant (EV {money(pool_ci[1])}, p={pool_p:.3f}).")
        t.append(edge)
    else:
        edge = (f"<b>Negative expectancy this month:</b> day EV {money(day_ev)} "
                f"(95% CI [{money(ci[0])}, {money(ci[2])}]) — the month destroyed value on average.")
        if has_pooled and not positional and pool_ci[1] <= 0:
            # a workbook pools its earlier months automatically, so the pooled window can be negative too
            edge += (f" Over the pooled {len(pooled_pnl)}-day window the day EV is also negative "
                     f"(EV {money(pool_ci[1])}/day, p={pool_p:.3f}) — no edge is demonstrated; review regime "
                     "and sizing before continuing.")
        elif has_pooled:
            edge += (f" Your baseline edge over the pooled {len(pooled_pnl)}-day window is still positive "
                     f"(EV {money(pool_ci[1])}/day, p={pool_p:.3f})")
            if pool_p < 0.05:
                edge += (" and significant — so this is a negative-EV drawdown, not proof the edge is gone; "
                         "the edge is regime-dependent.")
            else:
                edge += (", though not significant — treat this as a warning sign to review regime and sizing "
                         "before continuing.")
        t.append(edge)

    # 3. Short leg (the engine)
    if short_row is not None and not np.isnan(short_row["avg_loss"]):
        if short_row["ev"] > 0:
            strength = "barely positive (a razor-thin " if 0 < margin < 0.02 else "a "
            t.append(f"<b>The engine held up:</b> the short leg won {pct(short_row['win_rate'],1,False)} "
                     f"(avg win {money(short_row['avg_win'])} vs avg loss {money(short_row['avg_loss'])}) — "
                     f"{strength}<b>{pct(margin,1,False)}</b> margin over breakeven; insurance cost "
                     f"{money(long_row['ev'])}/position.")
        else:
            t.append(f"<b>The short leg itself lost money</b> (won {pct(short_row['win_rate'],1,False)}, "
                     f"avg win {money(short_row['avg_win'])} vs avg loss {money(short_row['avg_loss'])}, margin "
                     f"{pct(margin,1,False)}). This is the rare failure mode — check strike distance, size per "
                     f"spread, and VIX regime before resuming.")

    # 4. Daily loss cap
    if worst_day_val < -1000:
        if month_positive:
            t.append(f"<b>Daily loss cap:</b> a −$1,000 cap would have trimmed the worst day ({money(worst_day_val)}) "
                     f"and preserved more of the month.")
        else:
            t.append(f"<b>Daily loss cap:</b> a −$1,000 cap would have cut the loss from {money(total_pnl)} to "
                     f"≈ <b>{money(capped_total)}</b> — the damage is one or two outsized days.")
    else:
        t.append(f"<b>Discipline:</b> no day breached −$1,000 (worst {money(worst_day_val)}) — "
                 f"{'the gain is spread, not one lucky day' if month_positive else 'the loss is spread, not a single blow-up'}.")

    # 5. Re-entry
    if len(stop_rows):
        re_net = float(reentry["pnl_after"].sum())
        helped = int((reentry["pnl_after"] > 0).sum())
        if re_net > 0:
            t.append(f"<b>Re-entry worked:</b> trades after the {len(stop_rows)} stop events recovered "
                     f"{money(re_net)} (helped on {helped} of {len(stop_rows)}). Keep it, but cap size after a stop.")
        else:
            t.append(f"<b>Re-entry hurt:</b> trades after the {len(stop_rows)} stop events lost {money(re_net)} — "
                     f"on this tape re-entering amplified the damage; sit out after a stop until the day is back "
                     f"within −$500.")

    # 6. Regime attribution
    if regime_tbl is not None and not regime_tbl.empty:
        rr = regime_tbl[(regime_tbl["days"] > 0) & regime_tbl["net_ev"].notna()]
        if len(rr):
            if month_positive:
                best = rr.sort_values("net_ev", ascending=False).iloc[0]
                t.append(f"<b>Regime edge:</b> best days came in {best['regime']} (EV {money(best['net_ev'])}/day) — "
                         f"consider scaling size with vol.")
            else:
                worst = rr.sort_values("net_ev").iloc[0]
                t.append(f"<b>Regime attribution:</b> losses concentrated in {worst['regime']} "
                         f"(net {money(worst['net_pnl'])}) — consider reducing or stopping in that regime.")

    # 7. Unpaired short
    if len(unpaired):
        t.append(f"<b>Verify {len(unpaired)} unpaired short</b> on {unpaired.iloc[0]['date']} — should be a spread leg.")

    # 8. Tail discipline
    if len(stress):
        tail_cost = stress[stress["gap"] == 0.02]["pnl"].median()
        t.append(f"<b>Tail discipline:</b> a −2% SPX close would cost ≈ {money(abs(tail_cost))} on the day's book "
                 f"if held to expiry — the spread bounds it, but keep widths ≤ 30 pts and size so a −2% day is survivable.")

    # 9. Losing-month recovery checklist
    if not month_positive:
        t.append("<b>Before resuming:</b> check (a) the VIX regime of the losing days, (b) strike distance — were "
                 "stops hit by small moves?, and (c) size per spread vs the −$1,000 daily cap.")

    takeaways = "<ul>" + "".join(f"<li>{x}</li>" for x in t) + "</ul>"
    P.append(section("12", "Key Takeaways & Recommendations", takeaways))

    # ---- Assemble page ----
    body = (provenance_box(inputs.source) if inputs.source else "") + "".join(P)
    month_label = f"{label} · {span_text}" if label else span_text
    meta = (f"{inputs.account_label} · {month_start.strftime('%Y-%m-%d')} → {month_end.strftime('%Y-%m-%d')} · "
            f"{n_trades} trades · {n_contracts} contracts · risk-free {rf * 100:.0f}%")
    foot = (f"Generated {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')} by spx_trade_desk.tradelog.report.generate_report. "
            "Analysis is informational, not investment advice. Statistics are computed from your QFX "
            "statement with bull-put-credit-spread reconstruction (position direction from execution order). "
            "Monthly returns/Sharpe are computed on the month's starting balance (file-starting balance plus "
            "cumulative PnL through the prior month).")
    if not positional:
        foot = (f"Generated {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')} by spx_trade_desk.tradelog.report.generate_report. "
                "Analysis is informational, not investment advice. Statistics are computed from your trade-log "
                "workbook (SPX/SPXW option rows, dates only), so position direction, stops and tail stress are "
                "omitted. Monthly returns/Sharpe are computed on the month's starting capital (the workbook's "
                "starting capital plus cumulative PnL through the prior month).")
    html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(label or month_label)} — Trading Report</title>
<style>{CSS}</style>
</head>
<body>
<div class="viz">
  <h1>{esc(label or month_label)} — SPX 0DTE Bull Put Credit Spread Report</h1>
  <div class="meta">{esc(meta)}</div>
  {body}
  <div class="foot">{esc(foot)}</div>
</div>
</body>
</html>"""

    # ---- structured report data (for MCP / programmatic callers) ----
    regime_records = []
    _rt = m.get("vix_regime_table")
    if _rt is not None and not _rt.empty:
        regime_records = [_clean_dict(r) for r in _rt.to_dict("records")]
    weekly_records = [_clean_dict(r) for r in weekly.to_dict("records")] if not weekly.empty else []
    gap_records = []
    if len(stress):
        for g in gaps:
            sub = stress[stress["gap"] == g]
            if len(sub):
                gap_records.append({
                    "gap": _clean(g),
                    "median_day": _clean(sub["pnl"].median()),
                    "p90_day": _clean(np.percentile(sub["pnl"], 90)),
                    "worst_day": _clean(sub["pnl"].min()),
                })
    short_margin = None
    if short_row is not None and not np.isnan(short_row.get("avg_loss", np.nan)):
        short_margin = margin
    report_data = {
        "title": label or month_label,
        "month": window_label,
        "date_range": {"start": month_start.strftime("%Y-%m-%d"),
                       "end": month_end.strftime("%Y-%m-%d")},
        "initial_capital": _clean(initial_capital),
        "total_pnl": _clean(total_pnl),
        "return_pct": _clean(total_pnl / initial_capital),
        "total_trades": _clean(n_trades),
        "total_contracts": _clean(n_contracts),
        "trading_days": _clean(len(daily)),
        "positive_days": _clean(pos_days),
        "negative_days": _clean(neg_days),
        "best_day": _clean(m.get("max_gain")),
        "worst_day": _clean(m.get("max_loss")),
        "day_ev": _clean(day_ev),
        "risk": {
            "sharpe": _clean(m.get("sharpe")),
            "sortino": _clean(m.get("sortino")),
            "daily_std": _clean(m.get("std_daily")),
            "commission_drag": _clean(m.get("commission_drag")),
            "max_recovery_days": _clean(m.get("max_recovery_days")),
        },
        "spx": {
            "period_return": _clean(m.get("spx_period_return")),
            "return_delta": _clean(m.get("return_delta_vs_spx")),
            "corr": _clean(m.get("spx_corr")),
            "beta": _clean(m.get("spx_beta")),
            "alpha": _clean(m.get("spx_alpha")),
        },
        "vix_regimes": regime_records,
        "weekly": weekly_records,
        "spreads": {
            "short_positions": _clean(short_row["n"]) if short_row is not None else None,
            "long_positions": _clean(long_row["n"]) if long_row is not None else None,
            "paired": _clean(n_paired),
            "unpaired_count": _clean(len(unpaired)),
            "unpaired_dates": [_clean(pd.Timestamp(d)) for d in unpaired["date"]] if len(unpaired) else [],
            "width_median": _clean(widths.median()) if len(widths) else None,
            "credit_median": _clean(credits.median()) if len(credits) else None,
            "max_loss_median": _clean(maxloss.median()) if len(maxloss) else None,
            "short_leg": _clean_dict(short_row.to_dict()) if short_row is not None else None,
            "long_leg": _clean_dict(long_row.to_dict()) if long_row is not None else None,
        },
        "edge": {
            "day_ev": _clean(day_ev),
            "ci": [_clean(x) for x in ci],
            "p_value": _clean(p_val),
            "short_margin": _clean(short_margin),
            "pooled": ({
                "days": _clean(len(pooled_pnl)),
                "window": pool_window,
                "ev": _clean(pool_ci[1]),
                "ci": [_clean(x) for x in pool_ci],
                "p_value": _clean(pool_p),
            } if has_pooled else None),
        },
        "stops": {
            "count": _clean(len(stop_rows)),
            "total_loss": _clean(float(stop_rows["total_pnl"].sum())) if len(stop_rows) else 0.0,
            "reentry_net": _clean(float(reentry["pnl_after"].sum())) if len(reentry) else 0.0,
            "helped_count": _clean(int((reentry["pnl_after"] > 0).sum())) if len(reentry) else 0,
            "events": [_clean_dict(r) for r in reentry.to_dict("records")] if len(reentry) else [],
            "stop_days_mean_pnl": _clean(float(stop_day_pnl.mean())) if len(stop_day_pnl) else None,
            "normal_days_mean_pnl": _clean(float(normal_day_pnl.mean())) if len(normal_day_pnl) else None,
            "reconciliation": {
                "positions_per_day": _clean(positions_per_day),
                "position_ev": _clean(float(positions["total_pnl"].mean())) if len(positions) else None,
                "predicted_day_ev": _clean(day_ev_pred),
                "actual_day_ev": _clean(day_ev),
            },
        },
        "tail": {
            "gap_stress": gap_records,
            "monte_carlo": {k: _clean(v) for k, v in mc.items()} if mc else {},
            "worst_day": _clean(worst_day_val),
            "daily_cap": {"capped_total": _clean(capped_total), "flips_positive": bool(month_flips_positive)},
        },
        "kelly": {"binary": _clean(f_kelly), "half": _clean(half)},
        "cross_month": [_clean_dict(r) for r in cross_rows],
        "takeaways": [re.sub(r"<[^>]+>", "", x) for x in t],
    }
    if not positional:
        report_data["spreads"] = None
        report_data["stops"] = None
        report_data["kelly"] = None
        report_data["tail"]["gap_stress"] = None
        report_data["source"] = inputs.source
        report_data["unavailable_sections"] = [7, 9, 11]
    return html_doc, report_data


def main():
    ap = argparse.ArgumentParser(description="Generate a comprehensive HTML monthly trading report.")
    ap.add_argument("--monthly", required=True, help="QFX statement for the target month (or containing it), or an .xlsx trade-log workbook (day-level report).")
    ap.add_argument("--month", default=None, help="Filter to one calendar month, e.g. 2026-06.")
    ap.add_argument("--ytd", default=None, help="YTD (or prior-month) QFX for cross-month context (not used with a workbook).")
    ap.add_argument("--rf", type=float, default=0.04, help="Annual risk-free rate (decimal). Default 0.04.")
    ap.add_argument("--label", default=None, help="Report title label, e.g. 'July 2026'.")
    ap.add_argument("--offline", action="store_true", help="Use persisted market-data CSVs (no network fetch).")
    ap.add_argument("--out", default=str(REPORT_OUTPUT_DIR / "report.html"), help="Output HTML path.")
    args = ap.parse_args()

    doc, _ = build_report(args)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(doc, encoding="utf-8")
    print(f"Wrote {out} ({out.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
