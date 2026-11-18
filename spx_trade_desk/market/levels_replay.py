"""Levels replay: scores the dealer levels and the charm / vanna flow estimates on recorded chains.

Offline tooling (Phase 5). Each chain-library record is run through the same computation the live
publisher uses (``compute_gex`` -> ``positions_from_oi`` -> ``build_dealer``), with a fresh expected-move
anchor and ATM IV history per day, so the replay reproduces what the desk would have shown at that
minute. The SPX 1-minute bars are supplied as a CSV (the library holds no price series).

Level outcomes: a level is touched when a bar reaches within TOUCH_PTS of it inside the first WINDOW_MIN
bars after the record; it then holds when a close comes HOLD_PTS back on the original side, or breaks
when a close goes HOLD_PTS through it, both within WINDOW_MIN bars of the touch. Each level is compared
with a mirrored baseline (``spot - (level - spot)``), a level the same distance on the other side.

Bar times are matched to record minutes exactly (``YYYY-MM-DDTHH:MM``, ET): the bar stamped with the
record's minute is the base of every forward return, and only bars stamped after it can score a level.

Output goes to ``docs/experiments/`` (gitignored) by default. Aggregate results only belong in the docs.
"""

import argparse
import csv
import json
import sys
from bisect import bisect_right
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np

from spx_trade_desk.core.config import DEFAULT_RISK_FREE_RATE
from spx_trade_desk.market.atm import AtmIvHistory, ExpectedMoveAnchor
from spx_trade_desk.market.chain_publisher import tte_years
from spx_trade_desk.market.dealer_model import positions_from_oi
from spx_trade_desk.market.dealer_payload import build_dealer
from spx_trade_desk.market.gex import GEXResult, OptionData, compute_gex
from spx_trade_desk.market.hours import ET
from spx_trade_desk.resources import CHAIN_LIBRARY_DIR, EXPERIMENTS_DIR
from spx_trade_desk.sim.library import iter_records

TOUCH_PTS = 2.0                 # a bar this close to a level touches it
HOLD_PTS = 5.0                  # a close this far back on the original side holds; through it breaks
WINDOW_MIN = 30                 # bars after a record (and after a touch) that can score a level
DEDUPE_MIN = 30                 # a (type, round(level)) pair is evaluated at most once per this many minutes
HORIZONS = (15, 30, 60)         # forward return horizons, minutes
TOD_BUCKETS = (("open", "09:30", "11:00"), ("mid", "11:00", "14:00"), ("late", "14:00", "16:00"))
MIN_CORR_SAMPLES = 10           # fewer paired samples than this: no correlation
FLOW_METRICS = ("charm_flow_15m_usd", "charm_to_close_usd", "vanna_flow_est_usd")
OUTCOMES = ("hold", "break", "touch_only", "untouched")


def _et(ts: datetime) -> datetime:
    """Aware datetimes convert to ET; naive ones are taken as ET."""
    return ts.astimezone(ET) if ts.tzinfo else ts.replace(tzinfo=ET)


def _minute(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M")


def _bucket(hhmm: str) -> Optional[str]:
    for name, lo, hi in TOD_BUCKETS:
        if lo <= hhmm < hi:
            return name
    return None


def record_to_options(rec: dict) -> List[OptionData]:
    """The record's quote rows as the OptionData the live book would hold (IV kept as IB reports it)."""
    out: List[OptionData] = []
    for row in rec.get("rows") or []:
        k, right = row.get("k"), row.get("r")
        if k is None or right not in ("C", "P"):
            continue
        out.append(OptionData(strike=float(k), right=right, gamma=row.get("gamma"), delta=row.get("delta"),
                              open_interest=int(row.get("oi") or 0), volume=int(row.get("vol") or 0),
                              implied_vol=row.get("iv"), bid=row.get("bid"), ask=row.get("ask"),
                              last=row.get("last")))
    return out


def score_level(level: float, spot: float, bars_after: List[dict]) -> str:
    """``"hold" | "break" | "touch_only" | "untouched"`` for one level against the bars after the record."""
    support = level < spot
    for i, b in enumerate(bars_after[:WINDOW_MIN]):
        touched = b["low"] <= level + TOUCH_PTS if support else b["high"] >= level - TOUCH_PTS
        if touched:
            return _after_touch(level, support, bars_after[i:i + WINDOW_MIN])
    return "untouched"


def _after_touch(level: float, support: bool, window: List[dict]) -> str:
    side = 1.0 if support else -1.0      # +1: the original side is above the level
    for b in window:
        move = side * (b["close"] - level)
        if move <= -HOLD_PTS:
            return "break"
        if move >= HOLD_PTS:
            return "hold"
    return "touch_only"


def _levels_of(gex: GEXResult, dealer: Optional[dict]) -> Tuple[str, List[Tuple[str, float]]]:
    """The regime and the (type, level) pairs one record carries: zero gamma, walls, nodes and the open band."""
    regime = dealer["regime"] if dealer else "unknown"
    out: List[Tuple[str, float]] = []
    if dealer is not None:
        if dealer["zero_gamma"] is not None:
            out.append(("zero_gamma", dealer["zero_gamma"]))
        for node in dealer["nodes"]:
            out.append((f"node_{node['kind']}", node["strike"]))
        for key in ("high", "low"):
            if dealer["em"][key] is not None:
                out.append((f"em_{key}", dealer["em"][key]))
    if gex.call_wall is not None:
        out.append(("call_wall", float(gex.call_wall)))
    if gex.put_wall is not None:
        out.append(("put_wall", float(gex.put_wall)))
    return regime, out


def _compute(rec: dict, now: datetime, r: float, anchor: ExpectedMoveAnchor, iv_history: AtmIvHistory,
             day_bars: List[dict]) -> Tuple[GEXResult, Optional[dict]]:
    """The live publish for one record: the same steps as ``chain_publisher.publish_chain``."""
    options = record_to_options(rec)
    spot = float(rec["spot"])
    expiration = rec["expiry"]
    T = tte_years(expiration, now)
    gex_opts = [replace(o) for o in options]
    if sum(o.open_interest for o in gex_opts) == 0:
        for o in gex_opts:
            o.open_interest = o.volume
    gex = compute_gex(gex_opts, spot, time_to_expiry_years=T, risk_free_rate=r)
    gex.expiration = expiration
    gex.timestamp = now.isoformat()
    pos = positions_from_oi(gex)
    minute = _minute(now)
    bars_to_now = [b for b in day_bars if b["time"] <= minute]
    dealer, _ = build_dealer(gex, options, pos, T, r, now, anchor, iv_history, bars_to_now,
                             now.strftime("%Y-%m-%d"))
    return gex, dealer


def _forward_return(close_at: Dict[str, float], minute: str, now: datetime, horizon: int) -> Optional[float]:
    base = close_at.get(minute)
    later = close_at.get(_minute(now + timedelta(minutes=horizon)))
    if base is None or later is None or base == 0:
        return None
    return later / base - 1.0


def replay_day(records: Iterable[dict], bars: List[dict], r: float) -> dict:
    """Level outcomes and flow estimates for one day's records against that day's bars.

    Records are replayed in time order; a record whose expiry is not its own date is skipped (GTH
    publishes for the next day). Each record gets a fresh anchor and ATM IV history only once per day,
    so the open band and the IV change behave as they did live.
    """
    bars = sorted(bars, key=lambda b: b["time"])
    times = [b["time"] for b in bars]
    close_at = {b["time"]: float(b["close"]) for b in bars}
    anchor = ExpectedMoveAnchor()
    iv_history = AtmIvHistory()
    last_eval: Dict[Tuple[str, int], datetime] = {}
    levels: List[dict] = []
    flows: List[dict] = []

    stamped = []
    for rec in records:
        try:
            now = _et(datetime.fromisoformat(rec["ts"]))
        except (KeyError, TypeError, ValueError):
            continue
        if rec.get("expiry") != now.strftime("%Y%m%d") or not rec.get("spot"):
            continue
        stamped.append((now, rec))
    stamped.sort(key=lambda p: p[0])

    for now, rec in stamped:
        minute = _minute(now)
        gex, dealer = _compute(rec, now, r, anchor, iv_history, bars)
        spot = float(rec["spot"])
        i = bisect_right(times, minute)
        after = bars[i:]
        regime, pairs = _levels_of(gex, dealer)
        if after:
            for typ, level in pairs:
                key = (typ, round(level))
                prev = last_eval.get(key)
                if prev is not None and now - prev < timedelta(minutes=DEDUPE_MIN):
                    continue
                last_eval[key] = now
                mirror = spot - (level - spot)
                levels.append({"type": typ, "regime": regime, "level": level, "spot": spot,
                               "outcome": score_level(level, spot, after),
                               "baseline_outcome": score_level(mirror, spot, after),
                               "time": now.strftime("%H:%M")})
        bucket = _bucket(now.strftime("%H:%M"))
        if dealer is not None and bucket is not None:
            flows.append({"time": now.strftime("%H:%M"), "bucket": bucket,
                          "charm_flow_15m_usd": dealer["charm"]["flow_15m_usd"],
                          "charm_to_close_usd": dealer["charm"]["to_close_usd"],
                          "vanna_flow_est_usd": dealer["vanna"]["flow_est_usd"],
                          **{f"ret_{h}": _forward_return(close_at, minute, now, h) for h in HORIZONS}})
    return {"levels": levels, "flows": flows}


def _pearson(xs: List[float], ys: List[float]) -> Optional[float]:
    if len(xs) < MIN_CORR_SAMPLES:
        return None
    x, y = np.asarray(xs, dtype=float), np.asarray(ys, dtype=float)
    if x.std() == 0 or y.std() == 0:
        return None
    return round(float(np.corrcoef(x, y)[0, 1]), 4)


def summarize(days: List[dict]) -> dict:
    """Level rates per (type, regime) against the mirrored baseline, and flow correlations per (metric, horizon, bucket)."""
    by_level: Dict[str, List[dict]] = {}
    flow_rows: List[dict] = []
    for d in days:
        for ev in d["levels"]:
            by_level.setdefault(f"{ev['type']}|{ev['regime']}", []).append(ev)
        flow_rows.extend(d["flows"])

    levels = {}
    for key in sorted(by_level):
        evs = by_level[key]
        n = len(evs)

        def rate(field: str, value: str) -> float:
            return round(sum(1 for e in evs if e[field] == value) / n, 4)

        levels[key] = {
            "n": n,
            "touch_rate": round(sum(1 for e in evs if e["outcome"] != "untouched") / n, 4),
            "hold_rate": rate("outcome", "hold"),
            "break_rate": rate("outcome", "break"),
            "baseline_touch_rate": round(sum(1 for e in evs if e["baseline_outcome"] != "untouched") / n, 4),
            "baseline_hold_rate": rate("baseline_outcome", "hold"),
        }

    flows = {}
    for metric in FLOW_METRICS:
        for h in HORIZONS:
            for bucket, _, _ in TOD_BUCKETS:
                xs, ys = [], []
                for row in flow_rows:
                    x, y = row.get(metric), row.get(f"ret_{h}")
                    if row["bucket"] == bucket and x is not None and y is not None:
                        xs.append(float(x))
                        ys.append(float(y))
                flows[f"{metric}|{h}|{bucket}"] = _pearson(xs, ys)
    return {"levels": levels, "flows": flows}


def load_bars_csv(path) -> Dict[str, List[dict]]:
    """1-minute bars from a CSV (``ts, open, high, low, close``; ET, naive stamps taken as ET), grouped by YYYYMMDD."""
    out: Dict[str, List[dict]] = {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            ts = _et(datetime.fromisoformat(row["ts"].strip()))
            out.setdefault(ts.strftime("%Y%m%d"), []).append({
                "time": _minute(ts), "open": float(row["open"]), "high": float(row["high"]),
                "low": float(row["low"]), "close": float(row["close"])})
    for bars in out.values():
        bars.sort(key=lambda b: b["time"])
    return out


def _pct(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{100.0 * x:.1f}%"


def _markdown(summary: dict, days: int, dates: List[str]) -> str:
    lines = ["# Levels replay", "", f"Days replayed: {days} ({dates[0]} to {dates[-1]}). "
             "Aggregates only; the per-record data stays out of the repo.", "",
             "## Level outcomes", "",
             "| Level | Regime | n | Touch | Hold | Break | Baseline touch | Baseline hold |",
             "|---|---|---:|---:|---:|---:|---:|---:|"]
    for key, s in summary["levels"].items():
        typ, regime = key.split("|")
        lines.append(f"| {typ} | {regime} | {s['n']} | {_pct(s['touch_rate'])} | {_pct(s['hold_rate'])} | "
                     f"{_pct(s['break_rate'])} | {_pct(s['baseline_touch_rate'])} | "
                     f"{_pct(s['baseline_hold_rate'])} |")
    lines += ["", "## Flow correlations (Pearson)", "",
              f"Correlation is blank (n/a) below {MIN_CORR_SAMPLES} paired samples.", "",
              "| Metric | Horizon (min) | Bucket | Corr |", "|---|---:|---|---:|"]
    for key, corr in summary["flows"].items():
        metric, h, bucket = key.split("|")
        lines.append(f"| {metric} | {h} | {bucket} | {'n/a' if corr is None else f'{corr:.3f}'} |")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m spx_trade_desk.market.levels_replay",
                                 description="Replay recorded chains through the dealer levels and score them.")
    ap.add_argument("--date", nargs="+", required=True, metavar="YYYYMMDD", help="library day(s) to replay")
    ap.add_argument("--bars", required=True, metavar="PATH", help="1-minute SPX bars CSV (ts, open, high, low, close)")
    ap.add_argument("--library", default=str(CHAIN_LIBRARY_DIR), metavar="DIR", help="chain library directory")
    ap.add_argument("--out", default=None, metavar="DIR",
                    help="output directory (default: docs/experiments/levels-replay-FIRST-LAST, gitignored)")
    args = ap.parse_args(argv)

    dates = []
    for d in args.date:
        try:
            datetime.strptime(d, "%Y%m%d")
        except ValueError:
            ap.error(f"bad --date {d!r}; use YYYYMMDD")
        dates.append(d)
    dates = sorted(set(dates))

    bars = load_bars_csv(args.bars)
    library = Path(args.library)
    days, used = [], []
    for d in dates:
        path = library / f"{d}.jsonl.gz"
        if not path.exists():
            print(f"skip {d}: no library file", file=sys.stderr)
            continue
        if not bars.get(d):
            print(f"skip {d}: no bars in {args.bars}", file=sys.stderr)
            continue
        days.append(replay_day(iter_records(path), bars[d], DEFAULT_RISK_FREE_RATE))
        used.append(d)
    if not days:
        print("nothing to replay", file=sys.stderr)
        return 1

    summary = summarize(days)
    out = Path(args.out) if args.out else EXPERIMENTS_DIR / f"levels-replay-{used[0]}-{used[-1]}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(dict(summary, days=len(used), dates=used), indent=2) + "\n",
                                      encoding="utf-8")
    (out / "summary.md").write_text(_markdown(summary, len(used), used), encoding="utf-8")
    print(f"replayed {len(used)} day(s) -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
