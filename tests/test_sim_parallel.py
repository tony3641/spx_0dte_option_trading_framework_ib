# tests/test_sim_parallel.py
"""Process-pool parallelism for the sim pipeline.

The parallel path must be a pure speed change: same paths, same trials, same
payload as the serial path, bit for bit (the regression suite pins the serial
numbers, so any drift here would be a silent break).
"""
import os

import pytest

from spx_trade_desk.sim import jobs
from spx_trade_desk.sim import parallel
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
    from tests.test_sim_engine import _strategy
    jobs._STRATEGY_CACHE["T"] = _strategy()
    yield
    jobs.reset_registry()


def _payload(**kw):
    cfg = _cfg(**kw)
    bars = load_bars(cfg)
    return jobs.execute_pipeline(cfg, bars, lambda p, m: None, spot0=SPOT0)


# ---- worker-count resolution ----------------------------------------------
def test_resolve_workers_explicit_value():
    assert parallel.resolve_workers(SimRunConfig(strategy_name="T", n_workers=3), 10) == 3
    assert parallel.resolve_workers(SimRunConfig(strategy_name="T", n_workers=1), 10) == 1


def test_resolve_workers_capped_by_task_count():
    assert parallel.resolve_workers(SimRunConfig(strategy_name="T", n_workers=8), 2) == 2


def test_resolve_workers_auto_reads_env(monkeypatch):
    monkeypatch.setenv("SIM_WORKERS", "2")
    assert parallel.resolve_workers(SimRunConfig(strategy_name="T", n_paths=40), 10) == 2


def test_resolve_workers_auto_defaults_to_cpu_count(monkeypatch):
    monkeypatch.delenv("SIM_WORKERS", raising=False)
    monkeypatch.setattr(parallel.config, "SIM_WORKERS", 0)
    cfg = SimRunConfig(strategy_name="T", n_paths=10_000)
    assert parallel.resolve_workers(cfg, 100) == os.cpu_count()


def test_resolve_workers_auto_stays_serial_for_tiny_runs(monkeypatch):
    # Spawning a pool for a few hundred paths costs more than the run itself.
    monkeypatch.delenv("SIM_WORKERS", raising=False)
    monkeypatch.setattr(parallel.config, "SIM_WORKERS", 0)
    cfg = SimRunConfig(strategy_name="T", n_paths=400, chunk_size=100)
    assert parallel.resolve_workers(cfg, 4) == 1


def test_resolve_workers_auto_parallelizes_at_a_few_thousand_paths(monkeypatch):
    # Measured break-even: a worker's 250-path chunk (~4 s) dwarfs pool startup,
    # so 1000+ paths should already fan out.
    monkeypatch.delenv("SIM_WORKERS", raising=False)
    monkeypatch.setattr(parallel.config, "SIM_WORKERS", 0)
    cfg = SimRunConfig(strategy_name="T", n_paths=2000, chunk_size=250)
    assert parallel.resolve_workers(cfg, 8) == min(8, os.cpu_count() or 1)


def test_configured_workers_reads_env_then_config(monkeypatch):
    monkeypatch.setenv("SIM_WORKERS", "3")
    monkeypatch.setattr(parallel.config, "SIM_WORKERS", 5)
    assert parallel.configured_workers() == 3          # live env wins
    monkeypatch.delenv("SIM_WORKERS", raising=False)
    assert parallel.configured_workers() == 5          # .env-backed fallback
    monkeypatch.setattr(parallel.config, "SIM_WORKERS", 0)
    assert parallel.configured_workers() == 0          # 0 = auto


def test_n_workers_negative_rejected():
    with pytest.raises(ValueError, match="n_workers"):
        SimRunConfig(strategy_name="T", n_workers=-1).validate()


# ---- parallel == serial ----------------------------------------------------
def test_auto_workers_stay_serial_for_tiny_runs(monkeypatch):
    monkeypatch.delenv("SIM_WORKERS", raising=False)
    monkeypatch.setattr(parallel.config, "SIM_WORKERS", 0)
    assert _payload()["meta"]["workers"] == 1      # 40 paths -> not worth a pool


def test_parallel_payload_matches_serial():
    serial = _payload(n_workers=1)
    par = _payload(n_workers=2)
    assert par["cells"] == serial["cells"]
    assert par["spx_fan"] == serial["spx_fan"]
    assert par["meta"]["workers"] == 2 and serial["meta"]["workers"] == 1


def test_parallel_family_mode_matches_serial():
    # Family mode takes a different branch in compute_chunk (root + children, and
    # the child Strategy objects must survive pickling).
    from types import SimpleNamespace

    from spx_trade_desk.strategy.models import TriggerSpec
    from tests.test_sim_family import _child, _parent

    parent = _parent()
    child = _child(TriggerSpec(kind="parent_exit_reason", params={"reason": "stop_loss"}))
    state = SimpleNamespace(strategies={"P": parent, "C": child})

    def run(**kw):
        cfg = _cfg(strategy_name="P", mode="family", **kw)
        return jobs.execute_pipeline(cfg, load_bars(cfg), lambda p, m: None,
                                         spot0=SPOT0, state=state)

    serial, par = run(n_workers=1), run(n_workers=2)
    assert par["cells"] == serial["cells"]
    assert par["spx_fan"] == serial["spx_fan"]
    # Family mode reports every path as entered (placeholder trials carrying the
    # root+children total) — a sanity check that the family branch ran at all.
    assert par["cells"][0]["stats"]["entered"] == 40


def test_parallel_sweep_grid_order_and_values():
    kw = dict(sl_multipliers=[2.0, 6.0, float("inf")], strike_mode="dynamic_k",
              dynamic_k_values=[0.3, 0.6])
    serial = _payload(n_workers=1, **kw)
    par = _payload(n_workers=2, **kw)
    assert [c["sl_multiplier"] for c in par["cells"]] == [2.0, 2.0, 6.0, 6.0, "inf", "inf"]
    assert [c["k"] for c in par["cells"]] == [0.3, 0.6, 0.3, 0.6, 0.3, 0.6]
    assert par["cells"] == serial["cells"]


# ---- cancellation ----------------------------------------------------------
def test_parallel_honours_cancel():
    cfg = _cfg(sl_multipliers=[2.0, 6.0, float("inf")], n_workers=2)
    bars = load_bars(cfg)
    state = {"cancelled": False}

    def progress(p, m):
        state["cancelled"] = p >= 0.34

    payload = jobs.execute_pipeline(cfg, bars, progress, spot0=SPOT0,
                                        cancel_check=lambda: state["cancelled"])
    assert len(payload["cells"]) == 1


def test_pool_is_terminated_when_a_worker_fails(monkeypatch):
    # A worker crash must abort the pool, not drain every queued chunk first —
    # otherwise a failed run looks like a hang.
    cfg = _cfg(n_workers=2)
    bars = load_bars(cfg)

    class BoomPool:
        terminated = False

        def imap_unordered(self, *a, **k):
            raise RuntimeError("worker died")

        def terminate(self):
            self.terminated = True

        def close(self):
            pass

        def join(self):
            pass

    pool = BoomPool()
    monkeypatch.setattr(jobs, "spawn_pool", lambda n: pool)
    with pytest.raises(RuntimeError, match="worker died"):
        jobs.execute_pipeline(cfg, bars, lambda p, m: None, spot0=SPOT0)
    assert pool.terminated


# ---- worker determinism ----------------------------------------------------
def test_compute_chunk_is_deterministic():
    cfg = _cfg()
    bars = load_bars(cfg)
    model = jobs.calibrate(bars, cfg)
    from spx_trade_desk.strategy.models import Strategy
    strat = jobs._STRATEGY_CACHE["T"]
    ladder = jobs.build_ladder(SPOT0, cfg.ladder_range_pct)
    dyn = jobs.build_dynamics(model, cfg)
    cell = {"sl_multiplier": None, "k": None}
    a = parallel.compute_chunk((cfg, model, strat, [], ladder, dyn, cell, 0, 0, 20, SPOT0))
    b = parallel.compute_chunk((cfg, model, strat, [], ladder, dyn, cell, 0, 0, 20, SPOT0))
    assert [t.pnl for t in a["trials"]] == [t.pnl for t in b["trials"]]
    assert (a["spots"] == b["spots"]).all()
