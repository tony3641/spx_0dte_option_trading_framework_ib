"""Pricing validation harness (SP2 spec section 4.2).

    python -m spx_trade_desk.sim.validate DAY_FILE... [--tier auto|cold|thin|library]
        [--pricer z|legacy] [--root DIR] [--out PATH] [--store] [--vix1d-prev YYYYMMDD=VALUE]

For every record of a library day, the sim's pricing function prices the chain at the
record's real spot and time, with the ATM level the tier gives a sim run (the opening
anchor, never the real ATM), and is compared with the real chain:
1. the 10-wide bull-put credit for a 5/10/15/20-delta short, at the real pick's strikes;
2. the short strike the sim's delta picks vs the real chain's pick (IB delta);
3. the half-spread at the short strike (reported, not gated).
Every metric is scored twice: with the forecast ATM, and with the record's real ATM
(shape error only). Tier tables are built leave-one-out (the scored day is excluded).
Output: a console table and a JSON report under reports/output/ (gitignored).
"""
import json
import math
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

from spx_trade_desk.market.hours import ET
from spx_trade_desk.resources import CHAIN_LIBRARY_DIR, REPORT_OUTPUT_DIR
from spx_trade_desk.sim import data as sim_data
from spx_trade_desk.sim import library
from spx_trade_desk.sim.clock import CAL_TO_SIM, RTH_MINUTES, minutes_to_close, sim_rate, t_cal, t_sim
from spx_trade_desk.sim.library import (_parse_overrides, build_model_dict, fresh, iter_records,
                                        load_day, load_days, prior_closes, record_atm)
from spx_trade_desk.sim.pricing import RISK_FREE_RATE, bsm_put, bsm_put_delta
from spx_trade_desk.sim.pricing_model import (SIGMA_MAX, SIGMA_MIN, g_at, half_spread_at, ratio_at,
                                              tau_row_weights)
from spx_trade_desk.sim.pricing_tables import (N_TAU, TAU_LABELS, TIERS, load_cold, read_model_file,
                                               select_tables, tau_bucket)

REPORT_VERSION = 1
DELTAS = (0.05, 0.10, 0.15, 0.20)
WIDTH = 10.0
MIN_REAL_CREDIT = 0.15
DELTA_TOL = 0.03            # the real pick must sit within this of the target |delta|
BARS = (0.25,) * 4 + (0.40,) * 3   # median |credit error| allowed: before 15:00, then after
MIN_PAIRS = 5
STRIKE_TOL = 5.0
STRIKE_RATE = 0.8


class ZPricer:
    """The sim's pricing function at an arbitrary (spot, tau): the PricingModel lookups at
    the neutral path state (L = 1)."""
    name = "z"

    def __init__(self, tables, atm_open: float):
        self.tables, self.atm_open = tables, float(atm_open)
        self.rate = sim_rate(RISK_FREE_RATE)

    def price(self, spot: float, strikes, tau: float, atm: Optional[float] = None):
        t = self.tables
        lo, w = tau_row_weights(tau)
        lo, w = int(lo), float(w)
        row = t.f[lo] * (1.0 - w) + t.f[lo + 1] * w
        atm = self.atm_open * float(g_at(t.g, tau)) if atm is None else float(atm)
        sw = atm * math.sqrt(float(t_cal(max(tau, 0.5))))
        k = np.asarray(strikes, dtype=float)
        sigma = np.clip(atm * ratio_at(row, np.log(k / spot) / sw, sw) * CAL_TO_SIM,
                        SIGMA_MIN, SIGMA_MAX)
        T = float(t_sim(tau))
        mid = bsm_put(spot, k, T, self.rate, sigma)
        delta = bsm_put_delta(spot, k, T, self.rate, sigma)
        return mid, delta, half_spread_at(t.hs[int(tau_bucket(tau))], mid)


class LegacyPricer:
    """The pre-SP2 sim at its neutral state (baseline only): the SVI smile snapshot used
    directly as a sim-clock vol, and the old spread rule."""
    name = "legacy"

    def __init__(self):
        from spx_trade_desk.sim.calibrate import load_smile_snapshot
        self.smile, self.source = load_smile_snapshot()

    def price(self, spot: float, strikes, tau: float, atm: Optional[float] = None):
        k = np.asarray(strikes, dtype=float)
        m = np.log(k / spot)
        iv = np.clip(self.smile.iv(m), 0.01, 5.0)
        T = float(t_sim(tau))
        mid = bsm_put(spot, k, T, RISK_FREE_RATE, iv)
        delta = bsm_put_delta(spot, k, T, RISK_FREE_RATE, iv)
        return mid, delta, np.clip(self.smile.half_spread_atm * (1.0 + 8.0 * np.abs(m)), 0.01, 2.0)


def _real_puts(rec: dict) -> Dict[float, tuple]:
    """Fresh puts with a two-sided quote: {strike: (mid, half, IB delta)}."""
    out = {}
    for r in rec.get("rows") or []:
        if r.get("r") != "P" or r.get("k") is None or not fresh(r):
            continue
        bid, ask = r.get("bid"), r.get("ask")
        if bid is None or ask is None or not (bid > 0) or ask < bid:
            continue
        out[float(r["k"])] = ((bid + ask) / 2.0, (ask - bid) / 2.0, r.get("delta"))
    return out


def score_records(records, pricer, day: str, use_real_atm: bool = False) -> List[dict]:
    pairs = []
    for rec in records:
        if rec.get("expiry") != day or not rec.get("spot"):
            continue
        try:
            tau = minutes_to_close(datetime.fromisoformat(rec["ts"]))
        except (KeyError, ValueError, TypeError):
            continue
        if not (0.0 < tau <= RTH_MINUTES):
            continue
        spot = float(rec["spot"])
        puts = _real_puts(rec)
        if len(puts) < 3:
            continue
        atm = record_atm(rec.get("rows") or [], spot) if use_real_atm else None
        if use_real_atm and atm is None:
            continue
        strikes = np.array(sorted(puts))
        mid, delta, half = pricer.price(spot, strikes, tau, atm)
        sim = {k: (float(mid[i]), float(delta[i]), float(half[i])) for i, k in enumerate(strikes)}
        with_delta = [k for k in strikes if puts[k][2] is not None]
        if not with_delta:
            continue
        b = int(tau_bucket(tau))
        for target in DELTAS:
            real_k = min(with_delta, key=lambda k: abs(abs(puts[k][2]) - target))
            if abs(abs(puts[real_k][2]) - target) > DELTA_TOL:
                continue
            long_k = real_k - WIDTH
            if long_k not in puts:
                continue
            real_credit = puts[real_k][0] - puts[long_k][0]
            if real_credit < MIN_REAL_CREDIT:
                continue
            sim_credit = sim[real_k][0] - sim[long_k][0]
            sim_k = min(strikes, key=lambda k: abs(abs(sim[k][1]) - target))
            pairs.append({"bucket": b, "tau": round(tau, 2), "delta": target,
                          "real_credit": round(real_credit, 4), "sim_credit": round(sim_credit, 4),
                          "credit_err": (sim_credit - real_credit) / real_credit,
                          "strike_err": abs(float(sim_k) - float(real_k)),
                          "hs_err": abs(sim[real_k][2] - puts[real_k][1])})
    return pairs


def summarize(pairs: List[dict]) -> List[dict]:
    rows = []
    for b in range(N_TAU):
        ps = [p for p in pairs if p["bucket"] == b]
        row = {"bucket": TAU_LABELS[b], "bar": BARS[b], "n": len(ps), "credit_med_abs_err": None,
               "strike_within_5": None, "hs_med_abs_err": None, "pass": None}
        if ps:
            row["credit_med_abs_err"] = round(float(np.median([abs(p["credit_err"]) for p in ps])), 4)
            row["strike_within_5"] = round(float(np.mean([p["strike_err"] <= STRIKE_TOL for p in ps])), 4)
            row["hs_med_abs_err"] = round(float(np.median([p["hs_err"] for p in ps])), 4)
        if len(ps) >= MIN_PAIRS:
            row["pass"] = bool(row["credit_med_abs_err"] <= BARS[b]
                               and row["strike_within_5"] >= STRIKE_RATE)
        rows.append(row)
    return rows


def _date(day: str) -> date:
    return datetime.strptime(day, "%Y%m%d").date()


def run_harness(day_paths, tier: str = "auto", pricer_kind: str = "z", root=None, cold=None,
                overrides: Optional[Dict[str, float]] = None,
                daily: Optional[Dict[str, float]] = None) -> dict:
    root = Path(root) if root else CHAIN_LIBRARY_DIR
    cold = cold if cold is not None else load_cold()
    daily = sim_data.load_vix1d_daily() if daily is None else daily
    all_days = load_days(root)
    known = {d.day for d in all_days}
    extra = [d for d in (load_day(p) for p in day_paths) if d is not None and d.day not in known]
    prev = prior_closes(all_days + extra, daily, overrides)
    forecast, real, notes, resolved, scored = [], [], [], {}, []
    for path in map(Path, day_paths):
        day = path.name[:8]
        if pricer_kind == "legacy":
            pricer = LegacyPricer()
            resolved[day] = "legacy"
        else:
            others = [d for d in all_days if d.day != day]
            model = build_model_dict(others, prev) if others else None
            tables, info, _ = select_tables(model, cold, tier, prev.get(day), _date(day))
            resolved[day] = info["tier"]
            if prev.get(day) is None or not math.isfinite(tables.atm_vix1d_ratio):
                notes.append(f"{day}: no VIX1D prior close; skipped (pass --vix1d-prev)")
                continue
            pricer = ZPricer(tables, prev[day] / 100.0 * tables.atm_vix1d_ratio)
        records = list(iter_records(path))
        forecast += score_records(records, pricer, day)
        real += score_records(records, pricer, day, use_real_atm=True)
        scored.append(day)
    if pricer_kind == "legacy":
        notes.append("legacy pricer has no ATM input: both columns use its fixed smile")
    elif tier == "cold":
        notes.append("cold tier is not leave-one-out: a day the Cold default was built from "
                     "scores in-sample")
    fc = summarize(forecast)
    return {"v": REPORT_VERSION, "pricer": pricer_kind, "tier": tier, "resolved_tiers": resolved,
            "cold_provisional": bool(cold.provisional), "days": scored, "forecast": fc,
            "real_atm": summarize(real),
            "passed_buckets": sum(1 for r in fc if r["pass"]),
            "scored_buckets": sum(1 for r in fc if r["pass"] is not None),
            "generated": datetime.now(ET).isoformat(timespec="seconds"), "notes": notes}


def write_report(report: dict, out=None) -> Path:
    if out is None:
        stamp = datetime.now(ET).strftime("%Y%m%d_%H%M%S")
        out = REPORT_OUTPUT_DIR / f"sim_validate_{report['pricer']}_{report['tier']}_{stamp}.json"
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return out


def store_scores(report: dict, path=None) -> None:
    """Record the latest z-pricer score for the resolved tier in the model's metadata."""
    if report["pricer"] != "z":
        raise ValueError("only z-pricer scores are stored")
    path = Path(path) if path else library.MODEL_PATH
    model, err = read_model_file(path)
    if model is None:
        raise ValueError(err or f"{path} not found; build the library first")
    tiers = set(report["resolved_tiers"].values())
    key = tiers.pop() if len(tiers) == 1 else report["tier"]
    model.setdefault("scores", {})[key] = {"passed": report["passed_buckets"],
                                          "scored": report["scored_buckets"],
                                          "days": len(report["days"]), "at": report["generated"]}
    library.write_model(model, path)


def _pct(x) -> str:
    return "     n/a" if x is None else f"{x:8.1%}"


def format_report(report: dict) -> str:
    lines = [f"pricer={report['pricer']} tier={report['tier']} days={len(report['days'])}: "
             f"passed {report['passed_buckets']}/{report['scored_buckets']} scored buckets",
             f"{'bucket':>8} {'bar':>5} {'n':>5} {'credit':>8} {'K<=5':>8} {'half':>7} {'pass':>5}"
             f" | {'credit*':>8} {'K<=5*':>8} {'pass*':>5}"]
    for f, r in zip(report["forecast"], report["real_atm"]):
        hs = "    n/a" if f["hs_med_abs_err"] is None else f"{f['hs_med_abs_err']:7.3f}"
        lines.append(f"{f['bucket']:>8} {f['bar']:>5.2f} {f['n']:>5} {_pct(f['credit_med_abs_err'])}"
                     f" {_pct(f['strike_within_5'])} {hs} {str(f['pass']):>5}"
                     f" | {_pct(r['credit_med_abs_err'])} {_pct(r['strike_within_5'])} {str(r['pass']):>5}")
    lines.append("* = priced with the record's real ATM (shape error only)")
    lines += [f"note: {n}" for n in report["notes"]]
    return "\n".join(lines)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m spx_trade_desk.sim.validate",
                                 description="Score the sim's option pricing against recorded chains.")
    ap.add_argument("days", nargs="+", help="library day files (YYYYMMDD.jsonl.gz)")
    ap.add_argument("--tier", default="auto", choices=TIERS)
    ap.add_argument("--pricer", default="z", choices=("z", "legacy"))
    ap.add_argument("--root", default=None, help="library directory (default: data/chain_library)")
    ap.add_argument("--out", default=None, help="report path (default: reports/output/)")
    ap.add_argument("--store", action="store_true",
                    help="store the score in the library's pricing_model.json")
    ap.add_argument("--vix1d-prev", action="append", default=[], metavar="YYYYMMDD=VALUE")
    args = ap.parse_args(argv)
    report = run_harness(args.days, args.tier, args.pricer, args.root,
                         overrides=_parse_overrides(args.vix1d_prev))
    print(format_report(report))
    print(f"report: {write_report(report, args.out)}")
    if args.store:
        store_scores(report, Path(args.root) / library.MODEL_NAME if args.root else None)
    return 0 if report["passed_buckets"] == report["scored_buckets"] > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
