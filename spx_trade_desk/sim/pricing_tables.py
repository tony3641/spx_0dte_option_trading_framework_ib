"""Pricing tables for the simulator's z-model (SP2 spec sections 5-6).

- f(z, tau): IV / ATM IV on a fixed z grid, one row per time-to-close bucket.
- g(tau): the median intraday ATM curve, 1.0 at 09:45 (tau = 375).
- hs: the median put half-spread per (tau bucket, put-mid bucket).
- atm_vix1d_ratio: the median ATM(09:45) / VIX1D prior close.

All IV is in calendar units. pandas is imported inside the builders only, so a
spawned sim worker that unpickles the per-run pricer never loads it.
"""
import json
import math
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from spx_trade_desk.resources import CONFIG_DIR

TABLES_VERSION = 1
Z_GRID = np.linspace(-6.0, 3.0, 37)
Z_STEP = 0.25
Z_ATM = 24                                         # Z_GRID[24] == 0.0
N_Z = 37
TAU_EDGES = (300.0, 180.0, 120.0, 60.0, 30.0, 15.0)
TAU_LABELS = (">300", "300-180", "180-120", "120-60", "60-30", "30-15", "<15")
TAU_CENTERS = (345.0, 240.0, 150.0, 90.0, 45.0, 22.5, 7.5)
N_TAU = 7
MID_EDGES = (0.5, 1.0, 2.0, 3.0, 5.0, 10.0, 20.0)
N_MID = 8
G_TAU = (0.0, 5.0, 10.0, 15.0, 20.0, 30.0, 45.0, 60.0, 90.0, 120.0, 150.0, 180.0, 210.0,
         240.0, 270.0, 300.0, 330.0, 360.0, 375.0, 390.0)
G_NORM_TAU = 375.0                                 # 09:45
G_NODE_TOL = 10.0                                  # records within +/- 10 min feed a g node

MIN_NODE_POINTS = 3                                # a z node needs >= 3 points ...
MIN_NODE_RECORDS = 2                               # ... from >= 2 records
MIN_HS_POINTS = 5
Q_MAX = 2.0                                        # max |d(r^2)/dz| when extrapolating
RATIO_FLOOR = 0.3                                  # IV / ATM never below this
MIN_BUCKET_SWEEPS = 20
STALE_TRADING_DAYS = 10
LIBRARY_MIN_DAYS = 10
REGIME_MIN_DAYS = 5
REGIMES = ("lt12", "12-18", "18-25", "gt25")
TIERS = ("auto", "cold", "thin", "library")
COLD_PATH = CONFIG_DIR / "sim_pricing_default.json"

_NEG_TAU_EDGES = -np.asarray(TAU_EDGES)


def tau_bucket(tau) -> np.ndarray:
    """Bucket b covers (TAU_EDGES[b], TAU_EDGES[b-1]] minutes to the close."""
    return np.searchsorted(_NEG_TAU_EDGES, -np.asarray(tau, dtype=float), side="right")


def mid_bucket(mid) -> np.ndarray:
    return np.searchsorted(np.asarray(MID_EDGES), np.asarray(mid, dtype=float), side="right")


def _enc(a):
    a = np.asarray(a, dtype=float)
    if a.ndim == 1:
        return [round(float(x), 6) if math.isfinite(x) else None for x in a]
    return [_enc(r) for r in a]


def _dec(v, shape) -> np.ndarray:
    a = np.array(v, dtype=float)                    # None -> nan
    if a.shape != shape:
        raise ValueError(f"pricing tables: expected shape {shape}, got {a.shape}")
    return a


@dataclass
class PricingTables:
    f: np.ndarray                 # (7, 37) IV / ATM IV; all-NaN row = no data
    f_sweeps: np.ndarray          # (7,) records per tau bucket
    g: np.ndarray                 # (20,) ATM curve on G_TAU, 1.0 at tau = 375
    hs: np.ndarray                # (7, 8) put half-spread
    atm_vix1d_ratio: float        # ATM(09:45) / (VIX1D prior close / 100); NaN = unknown
    n_days: int = 0
    provisional: bool = False

    def row_usable(self, b: int) -> bool:
        return bool(np.isfinite(self.f[b]).all() and np.isfinite(self.hs[b]).all())

    def level_usable(self) -> bool:
        return bool(np.isfinite(self.g).all()) and math.isfinite(self.atm_vix1d_ratio)

    def to_dict(self) -> dict:
        r = self.atm_vix1d_ratio
        return {"v": TABLES_VERSION, "z_grid": [-6.0, 3.0, Z_STEP], "tau_edges": list(TAU_EDGES),
                "mid_edges": list(MID_EDGES), "g_tau": list(G_TAU),
                "f": _enc(self.f), "f_sweeps": [int(x) for x in self.f_sweeps],
                "g": _enc(self.g), "hs": _enc(self.hs),
                "atm_vix1d_ratio": round(float(r), 6) if math.isfinite(r) else None,
                "n_days": int(self.n_days), "provisional": bool(self.provisional)}

    @classmethod
    def from_dict(cls, d: dict) -> "PricingTables":
        if (d.get("v") != TABLES_VERSION or d.get("z_grid") != [-6.0, 3.0, Z_STEP]
                or d.get("tau_edges") != list(TAU_EDGES) or d.get("mid_edges") != list(MID_EDGES)
                or d.get("g_tau") != list(G_TAU)):
            raise ValueError("pricing tables use a different grid or version; rebuild them")
        r = d.get("atm_vix1d_ratio")
        return cls(f=_dec(d["f"], (N_TAU, N_Z)),
                   f_sweeps=np.array(d["f_sweeps"], dtype=int).reshape(N_TAU),
                   g=_dec(d["g"], (len(G_TAU),)), hs=_dec(d["hs"], (N_TAU, N_MID)),
                   atm_vix1d_ratio=float("nan") if r is None else float(r),
                   n_days=int(d.get("n_days", 0)), provisional=bool(d.get("provisional", False)))


def finish_row(nodes) -> np.ndarray:
    """Turn a row of node medians (NaN = no node) into a full f row.

    Interior gaps are interpolated, the interior is smoothed with [1/4, 1/2, 1/4] (the
    outermost nodes are kept so a linear row stays exact), the put wing is made
    non-increasing in z, and the row is extended past its first and last node linearly in
    r^2 (total variance at fixed T). The left slope is clamped to [-Q_MAX, 0], the right
    to [-Q_MAX, Q_MAX]; r is floored at RATIO_FLOOR. Fewer than 2 nodes: all NaN.
    """
    r = np.asarray(nodes, dtype=float).copy()
    idx = np.flatnonzero(np.isfinite(r))
    if len(idx) < 2:
        return np.full(N_Z, np.nan)
    lo, hi = int(idx[0]), int(idx[-1])
    span = np.arange(lo, hi + 1)
    r[span] = np.interp(span, idx, r[idx])
    if hi - lo >= 2:
        inner = r[lo:hi + 1].copy()
        r[lo + 1:hi] = 0.25 * inner[:-2] + 0.5 * inner[1:-1] + 0.25 * inner[2:]
    for i in range(min(Z_ATM, hi) - 1, lo - 1, -1):
        r[i] = max(r[i], r[i + 1])
    q = r * r
    left = min(max((q[lo + 1] - q[lo]) / Z_STEP, -Q_MAX), 0.0)
    right = min(max((q[hi] - q[hi - 1]) / Z_STEP, -Q_MAX), Q_MAX)
    q[:lo] = q[lo] + left * (Z_GRID[:lo] - Z_GRID[lo])
    q[hi + 1:] = q[hi] + right * (Z_GRID[hi + 1:] - Z_GRID[hi])
    return np.sqrt(np.maximum(q, RATIO_FLOOR ** 2))


def _build_f(days) -> Tuple[np.ndarray, np.ndarray]:
    import pandas as pd
    sweeps = np.zeros(N_TAU, dtype=int)
    frames = []
    for di, d in enumerate(days):
        rb = tau_bucket(d.rec_tau)
        sweeps += np.bincount(rb, minlength=N_TAU)
        if len(d.pt_z) == 0:
            continue
        node = np.rint((d.pt_z - Z_GRID[0]) / Z_STEP).astype(int)
        keep = (node >= 0) & (node < N_Z)
        frames.append(pd.DataFrame({"day": di, "rec": d.pt_rec[keep], "b": rb[d.pt_rec[keep]],
                                    "node": node[keep], "ratio": d.pt_ratio[keep]}))
    f = np.full((N_TAU, N_Z), np.nan)
    if frames:
        pts = pd.concat(frames, ignore_index=True)
        per_rec = pts.groupby(["b", "node", "day", "rec"])["ratio"].agg(["median", "size"]).reset_index()
        agg = per_rec.groupby(["b", "node"]).agg(med=("median", "median"), n_pts=("size", "sum"),
                                                 n_rec=("median", "size"))
        ok = agg[(agg["n_pts"] >= MIN_NODE_POINTS) & (agg["n_rec"] >= MIN_NODE_RECORDS)]
        for (b, node), med in ok["med"].items():
            f[int(b), int(node)] = float(med)
    for b in range(N_TAU):
        f[b] = finish_row(f[b])
    return f, sweeps


def _build_hs(days) -> np.ndarray:
    import pandas as pd
    hs = np.full((N_TAU, N_MID), np.nan)
    frames = [pd.DataFrame({"b": tau_bucket(d.rec_tau)[d.hs_rec], "j": mid_bucket(d.hs_mid),
                            "h": d.hs_half}) for d in days if len(d.hs_mid)]
    if frames:
        agg = pd.concat(frames, ignore_index=True).groupby(["b", "j"])["h"].agg(["median", "size"])
        for (b, j), row in agg.iterrows():
            if row["size"] >= MIN_HS_POINTS:
                hs[int(b), int(j)] = float(row["median"])
    cols = np.arange(N_MID)
    for b in range(N_TAU):
        ok = np.flatnonzero(np.isfinite(hs[b]))
        if len(ok):
            hs[b] = hs[b, ok[np.abs(cols[:, None] - ok[None, :]).argmin(axis=1)]]
    return hs


def _day_atm_at(d, tau0: float) -> Optional[float]:
    sel = np.abs(d.rec_tau - tau0) <= G_NODE_TOL
    return float(np.median(d.rec_atm[sel])) if sel.any() else None


def _covers_day(d) -> bool:
    """A full session that covers the open (09:45 +/- 10 min), midday and the last hour."""
    return (d.session_min == 390.0 and _day_atm_at(d, G_NORM_TAU) is not None
            and bool(((d.rec_tau >= 150.0) & (d.rec_tau <= 240.0)).any())
            and bool((d.rec_tau <= 60.0).any()))


def _build_level(days, prev: Dict[str, float], allow_partial: bool):
    """(g, atm_vix1d_ratio, partial). Partial mode (no full day, allow_partial) normalizes
    each day at its first record and keeps g flat (1.0) above that point."""
    full = [d for d in days if _covers_day(d)]
    partial = not full and allow_partial
    use = full or (list(days) if partial else [])
    g = np.full(len(G_TAU), np.nan)
    if not use:
        return g, float("nan"), False
    curves, ratios = [], []
    for d in use:
        tau0 = float(d.rec_tau.max()) if partial else G_NORM_TAU
        norm = _day_atm_at(d, tau0)
        c = np.full(len(G_TAU), np.nan)
        for i, tg in enumerate(G_TAU):
            if partial and tg >= tau0:
                c[i] = 1.0
                continue
            a = _day_atm_at(d, tg)
            if a is not None:
                c[i] = a / norm
        curves.append(c)
        p = prev.get(d.day)
        if p:
            ratios.append(norm / (p / 100.0))
    stack = np.vstack(curves)
    for i in range(len(G_TAU)):
        col = stack[:, i][np.isfinite(stack[:, i])]
        g[i] = float(np.median(col)) if len(col) else np.nan
    ok = np.isfinite(g)
    g = np.interp(np.asarray(G_TAU), np.asarray(G_TAU)[ok], g[ok])
    return g, (float(np.median(ratios)) if ratios else float("nan")), partial


def build_tables(days: Sequence, prev: Dict[str, float], allow_partial_g: bool = False) -> PricingTables:
    """Tables from extracted days. ``prev`` maps YYYYMMDD -> VIX1D prior close (percent)."""
    f, sweeps = _build_f(days)
    g, ratio, partial = _build_level(days, prev, allow_partial_g)
    return PricingTables(f=f, f_sweeps=sweeps, g=g, hs=_build_hs(days), atm_vix1d_ratio=ratio,
                         n_days=len(days), provisional=partial)


def fill_empty_rows(t: PricingTables) -> PricingTables:
    """Copy the nearest usable row (ties: the earlier bucket) into rows with no data."""
    out = {}
    for name in ("f", "hs"):
        arr = getattr(t, name).copy()
        ok = [b for b in range(N_TAU) if np.isfinite(arr[b]).all()]
        if ok:
            for b in range(N_TAU):
                if b not in ok:
                    arr[b] = arr[min(ok, key=lambda o: (abs(o - b), o))]
        out[name] = arr
    return replace(t, **out)


def regime_of(vix1d_prev: Optional[float]) -> Optional[str]:
    """VIX1D prior-close regime: <12, 12-18, 18-25, >=25; None when unknown."""
    if vix1d_prev is None or not (vix1d_prev > 0):
        return None
    if vix1d_prev < 12.0:
        return "lt12"
    if vix1d_prev < 18.0:
        return "12-18"
    if vix1d_prev < 25.0:
        return "18-25"
    return "gt25"


def load_cold(path=None) -> PricingTables:
    """The tracked Cold default; it must be complete (it is every fallback's last level)."""
    path = Path(path) if path else COLD_PATH
    t = PricingTables.from_dict(json.loads(path.read_text(encoding="utf-8")))
    if not (all(t.row_usable(b) for b in range(N_TAU)) and t.level_usable()):
        raise ValueError(f"{path.name} is incomplete; regenerate it with "
                         f"'python -m spx_trade_desk.sim.library build --write-default'")
    return t


def read_model_file(path) -> Tuple[Optional[dict], Optional[str]]:
    """(model, None); (None, None) when the file is absent; (None, warning) when unreadable."""
    path = Path(path)
    if not path.exists():
        return None, None
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            raise ValueError("not a JSON object")
        return d, None
    except (OSError, ValueError) as e:
        return None, f"pricing: cannot read {path.name} ({e}); using the Cold default"


def _busdays(day: str, today: date) -> int:
    return int(np.busday_count(f"{day[:4]}-{day[4:6]}-{day[6:]}", today.isoformat()))


def select_tables(model: Optional[dict], cold: PricingTables, tier: str = "auto",
                  vix1d_prev: Optional[float] = None, today: Optional[date] = None):
    """Tables for one run (spec section 6.2) -> (tables, info, warnings).

    Levels: library (the run's VIX1D regime; >= REGIME_MIN_DAYS days in it and
    >= LIBRARY_MIN_DAYS in the library) -> thin (pooled, >= 1 day) -> cold. A tau bucket
    with < MIN_BUCKET_SWEEPS sweeps (or no usable row) falls back one level at a time;
    g and the VIX1D ratio come from the first level that has them.
    """
    if tier not in TIERS:
        raise ValueError(f"pricing_tier must be one of {TIERS}")
    warnings: List[str] = []
    regime = regime_of(vix1d_prev)
    pooled = reg = None
    if model is not None:
        try:
            pooled = PricingTables.from_dict(model["pooled"])
            if regime is not None and regime in (model.get("regimes") or {}):
                reg = PricingTables.from_dict(model["regimes"][regime])
        except (KeyError, TypeError, ValueError) as e:
            warnings.append(f"pricing: model file unusable ({e}); using the Cold default")
            model = pooled = reg = None
    days = int(model.get("days", 0)) if model else 0
    lib_ok = (reg is not None and days >= LIBRARY_MIN_DAYS
              and int((model.get("regime_days") or {}).get(regime, 0)) >= REGIME_MIN_DAYS)
    levels = [("library", reg if lib_ok else None),
              ("thin", pooled if pooled is not None and pooled.n_days >= 1 else None),
              ("cold", cold)]
    chain = [(n, t) for n, t in levels[{"auto": 0, "library": 0, "thin": 1, "cold": 2}[tier]:]
             if t is not None]
    resolved = chain[0][0]
    if tier in ("library", "thin") and resolved != tier:
        why = " (no VIX1D prior close for the run)" if tier == "library" and regime is None else ""
        warnings.append(f"pricing: {tier} tier unavailable{why}; using {resolved}")
    f = np.empty((N_TAU, N_Z))
    hs = np.empty((N_TAU, N_MID))
    sweeps = np.zeros(N_TAU, dtype=int)
    used: List[str] = []
    for b in range(N_TAU):
        for name, t in chain:
            if name == "cold" or (t.f_sweeps[b] >= MIN_BUCKET_SWEEPS and t.row_usable(b)):
                f[b], hs[b], sweeps[b] = t.f[b], t.hs[b], t.f_sweeps[b]
                used.append(name)
                break
    level_name, level = next((n, t) for n, t in chain if t.level_usable())
    fallback = [f"{TAU_LABELS[b]}->{used[b]}" for b in range(N_TAU) if used[b] != resolved]
    provisional = any(t.provisional for n, t in chain if n in used or n == level_name)
    tables = PricingTables(f=f, f_sweeps=sweeps, g=level.g.copy(), hs=hs,
                           atm_vix1d_ratio=level.atm_vix1d_ratio, n_days=chain[0][1].n_days,
                           provisional=provisional)
    last = model.get("last_capture") if model else None
    stale = bool(last and resolved != "cold" and today is not None
                 and _busdays(last, today) > STALE_TRADING_DAYS)
    scores = ((model or {}).get("scores") or {}).get(resolved)
    score_txt = (f"harness {scores.get('passed')}/{scores.get('scored')} buckets passed"
                 if scores else "no harness score")
    warnings.append(f"pricing: tier {resolved}, {len(fallback)} of {N_TAU} tau buckets fell back, "
                    f"{days} library days, last capture {last or 'none'}, {score_txt}")
    if stale:
        warnings.append(f"pricing: chain library is stale (no capture in over "
                        f"{STALE_TRADING_DAYS} trading days)")
    if provisional:
        warnings.append("pricing: the Cold default is provisional (built from too few captured days)")
    info = {"tier": resolved, "requested": tier, "regime": regime, "fallback_buckets": fallback,
            "days": days, "last_capture": last, "stale": stale, "provisional": provisional,
            "scores": scores}
    return tables, info, warnings
