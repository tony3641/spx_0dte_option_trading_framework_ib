# tests/test_sim_family.py
import numpy as np

from spx_trade_desk.sim.calibrate import CalibratedModel, DEFAULT_SMILE, GarchParams
from spx_trade_desk.sim.config import SimRunConfig
from spx_trade_desk.sim.engine import TrialResult, run_family, trigger_minutes
from spx_trade_desk.sim.paths import SimPaths
from spx_trade_desk.strategy.models import (Condition, ExitRules, StopLoss, Strategy, TriggerSpec)


def _model():
    return CalibratedModel(
        garch=GarchParams(omega=2e-10, alpha=0.05, gamma=0.10, beta=0.85, nu=6.0, converged=True),
        ushape=np.ones(390), sigma0=0.0005, smile=DEFAULT_SMILE, vix0=15.0, source="test")


def _parent(mult: float = 6.0):
    # credit is MIN-ONLY {min:3.0}: engine-mode combo credits for a 40-65pt bull put on the
    # 5100-6900 ladder at spot 6000 have an engine-only scale ~9-18pts (Conflict K-b); a
    # {min:0.30,max:0.45} band admits none. mult parameterized so the stop test can use 2.0
    # (K-c: stop_level = fill_credit*2 ~ 37 < 65pt width, so a crash stops the parent).
    return Strategy(name="P", direction="bull_put",
                    conditions=[
                        Condition(kind="short_delta", params={"min": 0.30, "max": 0.45}),
                        Condition(kind="spread_width", params={"min": 40, "max": 65}),
                        Condition(kind="credit", params={"min": 3.0}),
                        Condition(kind="entry_window", params={"start": "09:35", "end": "10:00"}),
                    ],
                    exit_rules=ExitRules(stop_loss=StopLoss(multiplier=mult)), budget=None)


def _child(trigger):
    return Strategy(name="C", direction="bull_put",
                    conditions=[
                        Condition(kind="short_delta", params={"min": 0.05, "max": 0.45}),
                        Condition(kind="spread_width", params={"min": 40, "max": 65}),
                        Condition(kind="credit", params={"min": 0.10}),
                    ],
                    exit_rules=ExitRules(stop_loss=StopLoss(multiplier=6.0)), budget=None,
                    parent_name="P",
                    subsequent_triggers=[trigger])


def _paths(n=6, crash_after=20):
    """Paths 0-1 crash to 5800 (K-g): below the parent's entry strikes (so the parent stops when
    both legs go ITM, mark=width=65 > stop_level 36.6) but still inside the ladder, so the child
    finds an OTM short with delta in [0.05,0.45] when it re-enters. Paths 2-5 stay quiet."""
    rng = np.random.default_rng(1)
    spots = np.full((n, 390), 6000.0)
    spots[:2, crash_after:] = 5800.0
    spots[2:] += rng.normal(0, 1.0, (n - 2, 390)).cumsum(axis=1) * 0.1
    return SimPaths(spots=spots, sigmas=np.full((n, 390), 0.0005))


def test_family_reenters_after_parent_stop():
    cfg = SimRunConfig(strategy_name="P", bar_size="1m")
    ladder = np.arange(5100.0, 6900.0 + 2.5, 5.0)
    child = _child(TriggerSpec(kind="parent_exit_reason",
                               params={"reason": "stop_loss"}))
    results, total = run_family(_model(), cfg, _parent(mult=2.0), [child], _paths(), ladder)
    parent = results["P"]
    stopped = [p for p, r in enumerate(parent) if r.entered and r.exit_reason == "stop"]
    assert stopped == [0, 1]
    child_res = results["C"]
    for p in stopped:
        c = child_res[p]
        assert c.entered, "child must re-enter after a parent stop"
        assert c.entry_minute > parent[p].exit_minute
        assert c.fill_credit > 0
    for p in range(2, 6):
        assert not child_res[p].entered     # no trigger fired on quiet paths


def test_family_total_pnl_sums_all_legs():
    cfg = SimRunConfig(strategy_name="P", bar_size="1m")
    ladder = np.arange(5100.0, 6900.0 + 2.5, 5.0)
    child = _child(TriggerSpec(kind="parent_exit_reason", params={"reason": "stop_loss"}))
    results, total = run_family(_model(), cfg, _parent(), [child], _paths(), ladder)
    # total is per-PATH (n,); expected must be per-path (K-f), NOT a scalar
    n = len(results["P"])
    expected = np.zeros(n)
    for name in results:
        for i, r in enumerate(results[name]):
            expected[i] += r.pnl if r.entered else 0.0
    np.testing.assert_allclose(total, expected, atol=1e-6)


def test_family_unrealized_pnl_trigger():
    cfg = SimRunConfig(strategy_name="P", bar_size="1m")
    ladder = np.arange(5100.0, 6900.0 + 2.5, 5.0)
    child = _child(TriggerSpec(kind="parent_unrealized_pnl", params={"loss_multiple": 0.05}))
    results, total = run_family(_model(), cfg, _parent(mult=2.0), [child], _paths(), ladder)
    parent = results["P"]
    # The crash drives the parent's MTM far past -0.05x credit and stops it out (K-e); the
    # child re-enters only AFTER that exit (live: a child waits for the parent to be flat).
    for p in (0, 1):
        assert parent[p].exit_reason == "stop"
        assert results["C"][p].entered
        assert results["C"][p].entry_minute > parent[p].exit_minute
    for p in range(2, 6):
        assert not results["C"][p].entered


def test_family_unrealized_pnl_trigger_never_fires_while_the_parent_stays_open():
    """Regression: the sim used to start the child on the breach bar while the parent was
    still held, a state the live engine never allows. _parent() keeps mult 6.0 and rides the
    crash to expiry, so no session is left for the child."""
    cfg = SimRunConfig(strategy_name="P", bar_size="1m")
    ladder = np.arange(5100.0, 6900.0 + 2.5, 5.0)
    child = _child(TriggerSpec(kind="parent_unrealized_pnl", params={"loss_multiple": 0.05}))
    results, _ = run_family(_model(), cfg, _parent(), [child], _paths(), ladder)
    assert results["P"][0].exit_reason == "expired"
    assert not any(r.entered for r in results["C"])


def _held(exit_reason, exit_minute, mtm_by_bar, *, fc=0.40, qty=1, entry=5, steps=60):
    mtm = np.full(steps, np.nan)
    for t, v in mtm_by_bar.items():
        mtm[t] = v
    return TrialResult(entered=True, entry_minute=entry, exit_minute=exit_minute, exit_reason=exit_reason,
                       short_strike=6000.0, long_strike=5995.0, width=5.0, qty=qty, fill_credit=fc,
                       exit_debit=0.0, pnl=0.0, mtm=mtm)


def _unrealized(**params):
    return _child(TriggerSpec(kind="parent_unrealized_pnl", params=params))


def test_unrealized_trigger_starts_the_child_the_bar_after_the_parent_exit():
    res = _held("stop", 20, {6: -10.0, 12: -50.0, 20: -90.0})        # breach at bar 12, exit at 20
    fired = trigger_minutes([res], _unrealized(loss_multiple=1.0), steps=60)
    assert fired[0] == 21


def test_unrealized_trigger_scales_the_threshold_by_the_spreads_held():
    # fc 0.40 x 100 x 3 spreads = $120 of credit: -$60 is 0.5x, -$130 is 1.08x
    shallow = _held("stop", 20, {10: -60.0, 20: -60.0}, qty=3)
    deep = _held("stop", 20, {10: -130.0, 20: -130.0}, qty=3)
    child = _unrealized(loss_multiple=1.0)
    assert trigger_minutes([shallow], child, steps=60)[0] == -1
    assert trigger_minutes([deep], child, steps=60)[0] == 21


def test_unrealized_trigger_supports_gain_multiple():
    winner = _held("take_profit", 30, {10: 20.0, 30: 50.0})            # +$50 on $40 credit = 1.25x
    assert trigger_minutes([winner], _unrealized(gain_multiple=1.0), steps=60)[0] == 31
    assert trigger_minutes([winner], _unrealized(gain_multiple=2.0), steps=60)[0] == -1


def test_unrealized_trigger_ignores_the_entry_bar_and_needs_a_threshold():
    res = _held("stop", 20, {5: -500.0, 10: -1.0, 20: -2.0})            # only the entry bar is deep
    assert trigger_minutes([res], _unrealized(loss_multiple=1.0), steps=60)[0] == -1
    breach = _held("stop", 20, {10: -500.0, 20: -2.0})
    assert trigger_minutes([breach], _unrealized(), steps=60)[0] == -1  # neither gain nor loss given


def test_unrealized_trigger_never_fires_for_a_parent_held_to_expiry():
    res = _held("expired", 59, {10: -500.0, 59: -500.0}, steps=60)     # exit bar is the last bar
    assert trigger_minutes([res], _unrealized(loss_multiple=1.0), steps=60)[0] == -1


def test_family_all_logic_raises():
    cfg = SimRunConfig(strategy_name="P", bar_size="1m")
    ladder = np.arange(5100.0, 6900.0 + 2.5, 5.0)
    child = _child(TriggerSpec(kind="parent_exit_reason", params={"reason": "stop_loss"}))
    child.trigger_logic = "all"
    import pytest
    with pytest.raises(ValueError):
        run_family(_model(), cfg, _parent(), [child], _paths(), ladder)
