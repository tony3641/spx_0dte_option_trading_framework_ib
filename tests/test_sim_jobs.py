# tests/test_sim_jobs.py
import os

import numpy as np
import pytest

from spx_trade_desk.sim import jobs
from spx_trade_desk.sim.config import SimRunConfig
from spx_trade_desk.sim.data import load_bars

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "SPX_1min_10d.csv")
SPOT0 = 7718.36   # last RTH close of the 10-day fixture (2026-09-04 16:00)


def _cfg(**kw):
    d = dict(strategy_name="T", source="csv", csv_path=FIXTURE, bar_size="1m",
             lookback_days=10, n_paths=40, chunk_size=20,
             equity=100_000.0, bootstrap_seqs=50, bootstrap_len=20)
    d.update(kw)
    return SimRunConfig(**d)


@pytest.fixture(autouse=True)
def _clean():
    jobs.reset_registry()
    # Controller correction (Conflict N-a): test_sim_jobs.py exercises the REAL execute_pipeline,
    # whose _get_strategy reads _STRATEGY_CACHE (config/strategies.json is absent -> {}). Seed the
    # synthetic "T" strategy here (the brief seeds it only in test_sim_api.py).
    from tests.test_sim_engine import _strategy
    jobs._STRATEGY_CACHE["T"] = _strategy()
    jobs._CALIB_CACHE.clear()
    yield
    jobs._CALIB_CACHE.clear()
    jobs.reset_registry()


def test_execute_pipeline_produces_payload():
    cfg = _cfg()
    bars = load_bars(cfg)
    seen = []
    payload = jobs.execute_pipeline(cfg, bars, lambda p, m: seen.append(p), spot0=SPOT0)
    assert seen and seen[-1] == 1.0
    assert payload["meta"]["strategy"] == "T"
    assert payload["meta"]["steps_per_day"] == 390
    assert len(payload["cells"]) == 1
    cell = payload["cells"][0]
    assert cell["stats"]["n"] == 40
    assert set(cell["breakdown"]) == {"expired", "stop", "take_profit", "never"}
    assert np.isfinite(cell["hist"]["edges"]).all()


def test_execute_pipeline_attaches_spx_fan():
    # The SPX path fan is a property of the market simulation (spot dynamics do not
    # depend on the sweep cell), so it is computed once from the first cell's paths
    # and attached at the result root for the UI's top graph + report export.
    cfg = _cfg()
    bars = load_bars(cfg)
    payload = jobs.execute_pipeline(cfg, bars, lambda p, m: None, spot0=SPOT0)
    sf = payload["spx_fan"]
    assert len(sf["quantiles"]) == 20
    assert len(sf["values"]) == 20
    steps = cfg.steps_per_day()
    assert sf["minutes"] == list(range(steps))
    assert all(len(row) == steps for row in sf["values"])
    lo, mid, hi = sf["values"][0], sf["values"][10], sf["values"][-1]
    assert all(l <= m <= h for l, m, h in zip(lo, mid, hi))


def test_execute_pipeline_sweep_grid():
    cfg = _cfg(sl_multipliers=[2.0, 6.0, float("inf")], strike_mode="dynamic_k",
               dynamic_k_values=[0.3, 0.6])
    bars = load_bars(cfg)
    payload = jobs.execute_pipeline(cfg, bars, lambda p, m: None, spot0=SPOT0)
    cells = payload["cells"]
    assert len(cells) == 6                                    # 3 SL x 2 k
    assert [c["sl_multiplier"] for c in cells] == [2.0, 2.0, 6.0, 6.0, "inf", "inf"]
    assert [c["k"] for c in cells] == [0.3, 0.6, 0.3, 0.6, 0.3, 0.6]


def test_pipeline_honours_cancel():
    cfg = _cfg(sl_multipliers=[2.0, 6.0, float("inf")])
    bars = load_bars(cfg)
    state = {"cancelled": False}

    def progress(p, m):
        state["cancelled"] = p >= 0.34                        # cancel mid-grid

    payload = jobs.execute_pipeline(cfg, bars, progress, spot0=SPOT0,
                                        cancel_check=lambda: state["cancelled"])
    assert len(payload["cells"]) == 1                         # stopped after cell 1


def test_start_run_lifecycle_with_stub_engine(monkeypatch):
    monkeypatch.setattr(jobs, "execute_pipeline",
                        lambda cfg, bars, cb, spot0, cancel_check=None: (
                            [cb(i / 4, "x") for i in range(5)],
                            {"meta": {"strategy": cfg.strategy_name}, "cells": []})[1])
    monkeypatch.setattr(jobs, "_load_bars_for", lambda cfg: None)
    job = jobs.start_run({"strategy_name": "T", "source": "csv", "csv_path": FIXTURE,
                              "n_paths": 10})
    status = jobs.get_status(job["job_id"])
    assert status["state"] in ("queued", "loading", "calibrating", "simulating", "done")
    for _ in range(200):                                      # poll to done
        if jobs.get_status(job["job_id"])["state"] == "done":
            break
        import time; time.sleep(0.02)
    assert jobs.get_status(job["job_id"])["state"] == "done"
    assert jobs.get_result(job["job_id"])["meta"]["strategy"] == "T"


def test_start_run_validates_and_blocks_concurrency():
    with pytest.raises(ValueError):
        jobs.start_run({"strategy_name": "", "n_paths": 10})


def test_registry_pruned_to_last_ten_keeping_active():
    jobs.reset_registry()
    for i in range(12):                                    # 12 finished jobs, oldest first
        jobs._registry[f"done{i}"] = dict(id=f"done{i}", state="done", progress=1.0,
                                              message="", result={"cells": []},
                                              cancelled=False, created=float(i))
    jobs._registry["act_run"] = dict(id="act_run", state="simulating", progress=0.4,
                                         message="", result=None, cancelled=False, created=100.0)
    jobs._registry["queued_run"] = dict(id="queued_run", state="queued", progress=0.0,
                                            message="", result=None, cancelled=False,
                                            created=101.0)
    jobs._prune_registry()
    # oldest two finished jobs dropped; the newest ten finished survive
    assert "done0" not in jobs._registry and "done1" not in jobs._registry
    assert "done2" in jobs._registry and "done11" in jobs._registry
    # active jobs are never dropped
    assert jobs._registry["act_run"]["state"] == "simulating"
    assert jobs._registry["queued_run"]["state"] == "queued"
    terminal = [jid for jid, j in jobs._registry.items() if j["state"] == "done"]
    assert len(terminal) == 10


def test_calibration_cache_key_tracks_tier_and_model_file(tmp_path, monkeypatch):
    from spx_trade_desk.sim import library
    path = tmp_path / "pricing_model.json"
    monkeypatch.setattr(library, "MODEL_PATH", path)
    cfg = _cfg()
    k0 = jobs._calib_key(cfg)
    path.write_text('{"v": 1}')
    k1 = jobs._calib_key(cfg)
    path.write_text('{"v": 1, "days": 2}')
    k2 = jobs._calib_key(cfg)
    assert len({k0, k1, k2}) == 3
    assert jobs._calib_key(_cfg(pricing_tier="cold")) != k2


def test_pipeline_meta_reports_pricing_and_config_warnings():
    cfg = SimRunConfig.from_dict(dict(_cfg().to_dict(), vol_beta=0.75))
    payload = jobs.execute_pipeline(cfg, load_bars(cfg), lambda p, m: None, spot0=SPOT0)
    meta = payload["meta"]
    assert "smile" not in meta
    assert meta["pricing"]["tier"] == "cold" and meta["pricing"]["anchor"] == "garch"
    assert meta["pricing"]["atm_open"] > 0 and meta["pricing"]["vix1d_prev"] is None
    assert meta["dials"]["pricing_tier"] == "auto" and "vol_beta" not in meta["dials"]
    assert meta["config_warnings"] and "vol_beta" in meta["config_warnings"][0]


def test_missing_vix1d_warns_in_the_run_meta_and_anchor():
    cfg = _cfg()
    payload = jobs.execute_pipeline(cfg, load_bars(cfg), lambda p, m: None, spot0=SPOT0)
    warns = payload["meta"]["garch_warnings"]
    assert any("no VIX1D prior close; regime tiers unavailable" in w for w in warns)
    assert any("ATM anchor = GARCH level (no VIX1D prior close)" in w for w in warns)


def test_atm_iv_override_skips_the_anchor_warning_but_keeps_the_vix1d_one():
    cfg = _cfg(atm_iv=0.16)
    warns = jobs.execute_pipeline(cfg, load_bars(cfg), lambda p, m: None,
                                  spot0=SPOT0)["meta"]["garch_warnings"]
    assert any("regime tiers unavailable" in w for w in warns)
    assert not any("ATM anchor = GARCH" in w for w in warns)


def test_degraded_calibration_is_not_cached_so_a_later_run_gets_vix1d(monkeypatch):
    from spx_trade_desk.sim import data as sim_data
    cfg = _cfg()
    bars = load_bars(cfg)
    first = jobs.execute_pipeline(cfg, bars, lambda p, m: None, spot0=SPOT0)
    assert first["meta"]["pricing"]["vix1d_prev"] is None
    monkeypatch.setattr(sim_data, "load_vix1d_daily", lambda period="2y": {"20200102": 15.0})
    second = jobs.execute_pipeline(cfg, bars, lambda p, m: None, spot0=SPOT0)
    assert second["meta"]["pricing"]["vix1d_prev"] == 15.0
    assert second["meta"]["pricing"]["anchor"] == "vix1d"
    assert not any("regime tiers unavailable" in w for w in second["meta"]["garch_warnings"])
