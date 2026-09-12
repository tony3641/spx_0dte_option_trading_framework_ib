"""Deterministic variant runner for agent-driven strategy tuning.

Executes NAMED KNOB VARIANTS of ONE live strategy (config/strategies.json)
through the MC stress simulator, without ever writing to the live config.

Design contract (methodology: one knob at a time, judgment-driven, no sweeps):
  - A spec JSON defines GLOBAL dataset / run / stress blocks shared by every
    variant plus a list of named knob variants. Because cfg is identical
    across variants (same seed, bars, n_paths, one sweep cell), the simulator's
    per-cell seed streams (SeedSequence(entropy=cfg.seed, spawn_key=(ci, ch)))
    replay identical spot paths -- free common random numbers, so metric
    differences are attributable to the knobs alone.
  - Overrides are injected in memory via the `state` stub consumed by
    sim_jobs._get_strategy; config/strategies.json is never touched here.
  - A "baseline" variant (the live config, no overrides) is always run first
    so every variant row has a paired comparison.

Usage (from the repo root):
    python sim_tune.py --strategy Experiment_1 --spec docs/experiments/<slug>/variants.json
    python sim_tune.py --spec spec.json --out docs/experiments/<slug> --smoke
    python sim_tune.py --spec spec.json --seeds 42,43,44     # robustness gate

Outputs into --out: results.csv (one row per variant x seed) and results.json
(full cell payloads + provenance). See .claude/skills/strategy-tuning/.
"""
import argparse
import copy
import csv
import json
import os
import re
import sys
import time
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from sim_config import SimRunConfig
from sim_data import load_bars
from sim_engine import _reject_unsupported_strategy
from sim_jobs import execute_pipeline
from strategy_models import Condition, StopLoss, Strategy, TakeProfit

ROOT = os.path.dirname(os.path.abspath(__file__))

# Dotted knob paths the runner knows how to apply. Deliberately a subset of what
# the sim consumes: trend / atm_iv gates and non-pct take-profit modes are NOT
# simulable (sim_engine._reject_unsupported_strategy / run_exits), so they are
# not tunable through this runner. Child triggers/parent fields are reserved for
# family mode (not implemented here yet).
KNOB_WHITELIST = (
    "short_delta.min", "short_delta.max",
    "spread_width.min", "spread_width.max",
    "credit.min", "credit.max",
    "entry_window.start", "entry_window.end",
    "volatility.vix_enabled", "volatility.vix_op", "volatility.vix_value",
    "exit.stop_multiplier",
    "take_profit.pct",
    "budget",
)

_DATASET_KEYS = ("source", "csv_path", "bar_size", "spot0", "lookback_days")
_RUN_KEYS = ("seed", "n_paths", "chunk_size", "equity", "ruin_threshold_pct",
             "bootstrap_seqs", "bootstrap_len")
# Market/pricing stress dials + fill model, frozen for the whole experiment.
_STRESS_KEYS = ("nu_override", "gamma_mult", "vol_beta", "flat_iv", "atm_iv",
                "vol_cap_mult", "skew_beta", "skew_t_gamma", "atm_budget",
                "budget_beta", "stop_extra", "tick_size", "ladder_range_pct")

CSV_COLUMNS = ("variant", "seed", "mean", "median", "std", "win_rate", "cvar5",
               "cvar1", "worst_day", "n", "entered", "never_entered_pct",
               "expired", "stop", "take_profit", "never", "ruin_prob",
               "dd_mean", "dd_p95", "dd_worst", "knobs")


def _whitelist_err(kind: str, bad: str, allowed) -> ValueError:
    return ValueError(
        f"unknown {kind}: {bad!r}\nallowed {kind}s:\n  " + "\n  ".join(allowed))


def _as_float(name: str, v: Any, lo: Optional[float] = None,
              hi: Optional[float] = None) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError(f"{name} must be a number, got {v!r}")
    f = float(v)
    if (lo is not None and f < lo) or (hi is not None and f > hi):
        bound = f">= {lo}" if hi is None else (f"<= {hi}" if lo is None else f"in [{lo}, {hi}]")
        raise ValueError(f"{name} must be {bound}, got {f}")
    return f


_HM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def _as_hhmm(name: str, v: Any) -> str:
    if not isinstance(v, str) or not (m := _HM_RE.match(v.strip())):
        raise ValueError(f"{name} must be a 24h 'HH:MM' time, got {v!r}")
    h, minute = int(m.group(1)), int(m.group(2))
    if h > 23 or minute > 59:
        raise ValueError(f"{name} is not a valid time: {v!r}")
    return f"{h:02d}:{minute:02d}"


def _condition(strategy: Strategy, kind: str) -> Condition:
    for c in strategy.conditions:
        if c.kind == kind:
            return c
    c = Condition(kind=kind, enabled=True, params={})
    strategy.conditions.append(c)
    return c


def _set_param(strategy: Strategy, kind: str, key: str, value: Any) -> None:
    """Set a condition param; None unsets it (extract_conditions' unset-bound semantics)."""
    cond = _condition(strategy, kind)
    if value is None:
        cond.params.pop(key, None)
    else:
        cond.params[key] = value


def apply_knobs(strategy: Strategy, knobs: Dict[str, Any]) -> Strategy:
    """Apply whitelist knobs to a strategy copy IN PLACE (caller deep-copies)."""
    for name, value in knobs.items():
        if name not in KNOB_WHITELIST:
            raise _whitelist_err("knob", name, KNOB_WHITELIST)
        if name == "short_delta.min":
            _set_param(strategy, "short_delta", "min", _as_float(name, value, 0.0, 1.0))
        elif name == "short_delta.max":
            _set_param(strategy, "short_delta", "max", _as_float(name, value, 0.0, 1.0))
        elif name == "spread_width.min":
            _set_param(strategy, "spread_width", "min", _as_float(name, value, 0.0))
        elif name == "spread_width.max":
            _set_param(strategy, "spread_width", "max", _as_float(name, value, 0.0))
        elif name == "credit.min":
            _set_param(strategy, "credit", "min", _as_float(name, value, 0.0))
        elif name == "credit.max":
            _set_param(strategy, "credit", "max", None if value is None else _as_float(name, value, 0.0))
        elif name == "entry_window.start":
            _set_param(strategy, "entry_window", "start", _as_hhmm(name, value))
        elif name == "entry_window.end":
            _set_param(strategy, "entry_window", "end", _as_hhmm(name, value))
        elif name == "volatility.vix_enabled":
            _set_param(strategy, "volatility", "vix_enabled", bool(value))
        elif name == "volatility.vix_op":
            if value not in ("above", "below", "range"):
                raise ValueError(f"{name} must be one of ('above', 'below', 'range'), got {value!r}")
            _set_param(strategy, "volatility", "vix_op", value)
        elif name == "volatility.vix_value":
            _set_param(strategy, "volatility", "vix_value", _as_float(name, value, 0.0))
        elif name == "exit.stop_multiplier":
            mult = _as_float(name, value, 0.0)
            if mult <= 0:
                raise ValueError(f"{name} must be > 0 (the stop triggers at credit x mult)")
            if strategy.exit_rules.stop_loss is None:
                strategy.exit_rules.stop_loss = StopLoss(multiplier=mult)
            else:
                strategy.exit_rules.stop_loss.multiplier = mult
        elif name == "take_profit.pct":
            if value is None:
                strategy.exit_rules.take_profit = None
            else:
                pct = _as_float(name, value, 0.0)
                if pct <= 0:
                    raise ValueError(f"{name} must be > 0, got {pct}")
                strategy.exit_rules.take_profit = TakeProfit(mode="pct_credit", value=pct)
        elif name == "budget":
            if value is None:
                strategy.budget = None
            else:
                strategy.budget = _as_float(name, value, 0.0)
    return strategy


def _check_keys(block: dict, allowed, kind: str) -> None:
    for k in block:
        if k not in allowed:
            raise _whitelist_err(f"{kind} key", k, allowed)


def load_spec(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        spec = json.load(f)
    return validate_spec(spec)


def validate_spec(spec: dict) -> dict:
    if not isinstance(spec, dict):
        raise ValueError("spec must be a JSON object")
    for key in ("strategy", "variants"):
        if key not in spec:
            raise ValueError(f"spec is missing required key {key!r}")
    if not isinstance(spec["strategy"], str) or not spec["strategy"]:
        raise ValueError("spec.strategy must be a non-empty string")
    _check_keys(spec, ("slug", "strategy", "dataset", "run", "stress", "variants"), "spec")
    dataset = dict(spec.get("dataset") or {})
    run = dict(spec.get("run") or {})
    stress = dict(spec.get("stress") or {})
    _check_keys(dataset, _DATASET_KEYS, "dataset")
    _check_keys(run, _RUN_KEYS, "run")
    _check_keys(stress, _STRESS_KEYS, "stress")
    if dataset.get("source") not in (None, "csv", "yfinance", "auto"):
        raise ValueError("dataset.source must be 'csv', 'yfinance', or 'auto' ('ib' is not implemented)")
    variants = spec["variants"]
    if not isinstance(variants, list) or not variants:
        raise ValueError("spec.variants must be a non-empty list")
    names = set()
    for v in variants:
        if not isinstance(v, dict) or not v.get("name"):
            raise ValueError("each variant needs a 'name'")
        _check_keys(v, ("name", "knobs"), "variant")
        if not isinstance(v.get("knobs", {}), dict):
            raise ValueError(f"variant {v['name']!r}: knobs must be an object")
        if v["name"] in names:
            raise ValueError(f"duplicate variant name: {v['name']!r}")
        if v["name"] == "baseline":
            raise ValueError("'baseline' is reserved for the live-config run the runner adds")
        names.add(v["name"])
    return spec


def _build_cfg(spec: dict, seed: int) -> SimRunConfig:
    kwargs = {}
    kwargs.update(spec.get("dataset") or {})
    kwargs.update(spec.get("run") or {})
    kwargs.update(spec.get("stress") or {})
    kwargs["seed"] = seed
    cfg = SimRunConfig(strategy_name=spec["strategy"], mode="single",
                       strike_mode="engine", **kwargs)
    cfg.validate()
    return cfg


def _summary_row(name: str, seed: int, cell: dict, knobs: dict) -> dict:
    s, b, dd = cell["stats"], cell["breakdown"], cell["dd"]
    return {
        "variant": name, "seed": seed,
        "mean": s["mean"], "median": s["median"], "std": s["std"],
        "win_rate": s["win_rate"], "cvar5": s["cvar5"], "cvar1": s["cvar1"],
        "worst_day": s["worst_day"], "n": s["n"], "entered": s["entered"],
        "never_entered_pct": s["never_entered_pct"],
        "expired": b.get("expired", 0), "stop": b.get("stop", 0),
        "take_profit": b.get("take_profit", 0), "never": b.get("never", 0),
        "ruin_prob": cell["ruin_prob"],
        "dd_mean": dd["mean"], "dd_p95": dd["p95"], "dd_worst": dd["worst"],
        "knobs": json.dumps(knobs, sort_keys=True),
    }


def _fan_equal(a: dict, b: dict) -> bool:
    return a.get("values") == b.get("values") and a.get("minutes") == b.get("minutes")


def run_experiment(spec: dict, out_dir: str,
                   strategies: Optional[Dict[str, Strategy]] = None,
                   seeds: Optional[List[int]] = None,
                   n_paths: Optional[int] = None,
                   smoke: bool = False,
                   log=print) -> dict:
    """Run baseline + spec variants per seed; write results.csv/.json into out_dir.

    `strategies` overrides the live store (tests / what-if on unsaved configs);
    defaults to strategy_store.load_strategies().
    """
    if strategies is None:
        from strategy_store import load_strategies
        strategies = load_strategies()
    spec = validate_spec(spec)
    name = spec["strategy"]
    if name not in strategies:
        raise ValueError(f"unknown strategy: {name!r} (have: {sorted(strategies)})")

    run = dict(spec.get("run") or {})
    run.pop("seed", None)  # seed comes from the seeds loop, never the spec
    if smoke:               # smoke forces the tiny profile (dry-run discipline)
        run.update(n_paths=400, chunk_size=100, bootstrap_seqs=100, bootstrap_len=30)
    spec = {**spec, "run": run}
    if n_paths is not None:
        spec = {**spec, "run": {**(spec.get("run") or {}), "n_paths": n_paths}}
    if seeds is None:
        seeds = [int((spec.get("run") or {}).get("seed", 42))]

    # Fail BEFORE any compute if the live strategy (or a variant) is not
    # faithfully simulable (non-bull_put, trend gate, atm_iv gate).
    baseline = copy.deepcopy(strategies[name])
    _reject_unsupported_strategy(baseline)
    variants = []
    for v in spec["variants"]:
        s = copy.deepcopy(strategies[name])
        apply_knobs(s, v.get("knobs") or {})
        _reject_unsupported_strategy(s)
        variants.append((v["name"], v.get("knobs") or {}, s))

    runs = [("baseline", {}, baseline)] + variants
    cfg0 = _build_cfg(spec, seed=seeds[0])
    bars = load_bars(cfg0)
    spot0 = float(cfg0.spot0) if cfg0.spot0 else float(bars.closes[-1])
    # The stub must carry the tuned strategy (else _get_strategy falls back to
    # the LIVE cached one and overrides silently vanish) plus the rest of the
    # tree for forward compatibility with family mode.
    state = SimpleNamespace(strategies={**strategies, name: baseline})

    rows: List[dict] = []
    payloads: List[dict] = []
    crn_ok = True
    t_start = time.time()
    for seed in seeds:
        cfg = _build_cfg(spec, seed=seed)
        log(f"seed {seed}: {len(runs)} runs x {cfg.n_paths} paths "
            f"({bars.source}, spot0={spot0:.2f})")
        ref_fan = None
        for i, (vname, knobs, strat) in enumerate(runs):
            state.strategies[name] = strat
            t0 = time.time()
            result = execute_pipeline(cfg, bars, lambda p, m: None, spot0=spot0,
                                      state=state)
            cell = result["cells"][0]
            fan = result["spx_fan"]
            if i == 0:
                ref_fan = fan
            elif not _fan_equal(ref_fan, fan):
                crn_ok = False
                log(f"WARNING: spx_fan differs across variants at seed {seed} "
                    f"({vname}) -- common random numbers broken; treat deltas as noisy")
            rows.append(_summary_row(vname, seed, cell, knobs))
            payloads.append(dict(name=vname, seed=seed, knobs=knobs,
                                 cell=cell, run_meta=result["meta"]))
            log(f"  {vname:<24} mean={cell['stats']['mean']:+9.2f} "
                f"win={cell['stats']['win_rate'] * 100:5.1f}% "
                f"cvar5={cell['stats']['cvar5']:+9.2f} "
                f"ruin={cell['ruin_prob'] * 100:4.1f}% "
                f"never={cell['stats']['never_entered_pct'] * 100:5.1f}% "
                f"({time.time() - t0:.1f}s)")

    os.makedirs(out_dir, exist_ok=True)
    results = dict(
        meta=dict(slug=spec.get("slug", ""), strategy=name,
                  dataset=spec.get("dataset") or {}, run=spec.get("run") or {},
                  stress=spec.get("stress") or {}, seeds=seeds,
                  spot0=spot0, smoke=smoke, crn_ok=crn_ok,
                  knob_whitelist=list(KNOB_WHITELIST),
                  generated_at=datetime.now().isoformat(timespec="seconds"),
                  total_elapsed_s=round(time.time() - t_start, 1)),
        variants=payloads)
    csv_path = os.path.join(out_dir, "results.csv")
    json_path = os.path.join(out_dir, "results.json")
    with open(csv_path, "w", encoding="utf-8", newline="\n") as f:
        w = csv.DictWriter(f, fieldnames=list(CSV_COLUMNS), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    with open(json_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(results, f, indent=2)
    log(f"wrote {csv_path}")
    log(f"wrote {json_path}")
    return results


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Run named strategy-knob variants through the MC simulator "
                    "(deterministic; never writes config/strategies.json).")
    ap.add_argument("--strategy", help="strategy name (default: spec.strategy)")
    ap.add_argument("--spec", required=True, help="variant spec JSON path")
    ap.add_argument("--out", help="output dir (default: docs/experiments/<slug|strategy>)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--seed", type=int, help="single seed (default: spec.run.seed or 42)")
    g.add_argument("--seeds", help="comma-separated seeds, e.g. 42,43,44 (robustness gate)")
    ap.add_argument("--n-paths", type=int, help="override run.n_paths")
    ap.add_argument("--csv", help="use this CSV for bars (overrides dataset)")
    ap.add_argument("--yfinance", action="store_true", help="use yfinance bars (overrides dataset)")
    ap.add_argument("--bar-size", help="override dataset.bar_size")
    ap.add_argument("--spot0", type=float, help="override dataset.spot0 (pin it for CRN)")
    ap.add_argument("--smoke", action="store_true",
                    help="tiny run (n_paths=400) for a fast deterministic dry-run")
    args = ap.parse_args(argv)

    spec = load_spec(args.spec)
    if args.strategy:
        spec["strategy"] = args.strategy
    dataset = dict(spec.get("dataset") or {})
    if args.csv:
        dataset.update(source="csv", csv_path=args.csv)
    if args.yfinance:
        dataset.update(source="yfinance")
    if args.bar_size:
        dataset["bar_size"] = args.bar_size
    if args.spot0 is not None:
        dataset["spot0"] = args.spot0
    spec["dataset"] = dataset
    seeds = None
    if args.seeds:
        seeds = [int(x) for x in args.seeds.split(",") if x.strip()]
    elif args.seed is not None:
        seeds = [args.seed]

    out_dir = args.out or os.path.join(
        ROOT, "docs", "experiments",
        spec.get("slug") or spec["strategy"].lower().replace(" ", "-"))
    try:
        run_experiment(spec, out_dir, seeds=seeds, n_paths=args.n_paths,
                       smoke=args.smoke)
    except (ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
