# tests/test_sim_regression.py
"""Pinned outputs of the z-model option pricer (SP2).

Each case runs the deterministic pipeline slice (calibrate -> paths -> entry -> exits) on
the 10-day 1-minute fixture (`SPX_1min_10d.csv`) with the tracked Cold default and no
VIX1D prior close (the GARCH anchor; conftest isolates both). Outputs must match the
committed npz with np.array_equal (exact float equality), not allclose. Re-pin with
write_baselines() only when the fixture, the Cold default or the model changes, in a
commit that says why.
"""
import os
import tempfile
from pathlib import Path

import numpy as np
import pytest

from spx_trade_desk.sim.calibrate import calibrate
from spx_trade_desk.sim.config import SimRunConfig
from spx_trade_desk.sim.data import load_bars
from spx_trade_desk.sim.engine import run_entry, run_exits
from spx_trade_desk.sim.paths import simulate_chunk
from spx_trade_desk.sim.pricing import build_ladder
from spx_trade_desk.strategy.models import Condition, ExitRules, StopLoss, Strategy

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
FIXTURE = os.path.join(FIXTURES, "SPX_1min_10d.csv")

CASES = {
    "z": {"pricing_tier": "cold"},
    "z_skew": {"pricing_tier": "cold", "skew_beta": 1.0},
}


def _strategy():
    return Strategy(
        name="T", direction="bull_put",
        conditions=[
            Condition(kind="short_delta", params={"min": 0.05, "max": 0.60}),
            Condition(kind="spread_width", params={"min": 5, "max": 50}),
            Condition(kind="credit", params={"min": 0.05}),
            Condition(kind="entry_window", params={"start": "09:35", "end": "14:00"}),
        ],
        exit_rules=ExitRules(stop_loss=StopLoss(multiplier=6.0)), budget=None)


def _cell(dials=None):
    """Deterministic pipeline slice: calibrate -> paths -> entry -> exits."""
    cfg = SimRunConfig(strategy_name="T", source="csv", csv_path=FIXTURE,
                       bar_size="1m", lookback_days=10, n_paths=60, seed=42)
    for k, v in (dials or {}).items():
        setattr(cfg, k, v)
    cfg.validate()
    bars = load_bars(cfg)
    model = calibrate(bars, cfg)
    spot0 = float(bars.closes[-1])
    ladder = build_ladder(spot0, cfg.ladder_range_pct)
    paths = simulate_chunk(model, cfg, spot0, cfg.n_paths,
                           np.random.SeedSequence(entropy=cfg.seed))
    entry = run_entry(model, cfg, _strategy(), paths, ladder)
    trials = run_exits(model, cfg, _strategy(), paths, ladder, entry)
    return entry, trials


def _npz(tag):
    return np.load(os.path.join(FIXTURES, f"sim_baseline_{tag}.npz"))


def _assert_cell_matches(z, entry, trials):
    assert np.array_equal(entry.entered, z["entered"])
    assert np.array_equal(entry.entry_minute, z["entry_minute"])
    assert np.array_equal(entry.short_idx, z["short_idx"])
    assert np.array_equal(entry.long_idx, z["long_idx"])
    assert np.array_equal(entry.fill_credit, z["fill"])
    assert np.array_equal(np.array([t.pnl for t in trials]), z["pnl"])
    if not np.isnan(z["mtm0"]).all():
        mtm = next(t.mtm for t in trials if t.mtm is not None)
        # entries land after bar 0, so the leading mtm minutes are NaN;
        # array_equal must treat them as equal rather than NaN != NaN.
        assert np.array_equal(mtm, z["mtm0"], equal_nan=True)


@pytest.mark.parametrize("tag", sorted(CASES))
def test_baseline_unchanged(tag):
    z = _npz(tag)
    assert z["entered"].any(), "a baseline with no entries pins nothing"
    _assert_cell_matches(z, *_cell(CASES[tag]))


def test_skew_beta_changes_the_pinned_fills():
    assert not np.array_equal(_npz("z")["fill"], _npz("z_skew")["fill"])


def write_baselines():
    """Re-pin (outside pytest):
    python -c "from tests.test_sim_regression import write_baselines; write_baselines()"
    Isolated like the test session: no local chain library, no VIX1D download."""
    from spx_trade_desk.sim import data as sim_data
    from spx_trade_desk.sim import library
    library.MODEL_PATH = Path(tempfile.mkdtemp()) / "pricing_model.json"
    sim_data.load_vix1d_daily = lambda period="2y": {}
    for tag, dials in CASES.items():
        entry, trials = _cell(dials)
        assert entry.entered.any(), f"{tag}: no entries; the baseline would pin nothing"
        mtm0 = next((t.mtm for t in trials if t.mtm is not None), np.full(390, np.nan))
        np.savez(os.path.join(FIXTURES, f"sim_baseline_{tag}.npz"),
                 entered=entry.entered, entry_minute=entry.entry_minute,
                 short_idx=entry.short_idx, long_idx=entry.long_idx,
                 fill=entry.fill_credit, pnl=np.array([t.pnl for t in trials]), mtm0=mtm0)
        print(f"{tag}: {int(entry.entered.sum())}/{len(trials)} entered")
