# tests/test_sim_api.py
"""API-level end-to-end: real app + real engine over HTTP (hermetic fixture data)."""
import os
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from spx_trade_desk import server

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "SPX_1min_10d.csv")


@pytest.fixture()
def client(monkeypatch):
    from spx_trade_desk.sim import jobs
    jobs.reset_registry()
    # keep the suite hermetic: never let the API path touch yfinance/IB
    monkeypatch.setattr("spx_trade_desk.sim.data.load_bars_yfinance",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disabled in tests")))
    # (brief Step 5 note) the synthetic "T" strategy lives only in tests; config/strategies.json
    # is absent, so _get_strategy would raise unknown-strategy. Seed the cache like test_sim_jobs.
    from tests.test_sim_engine import _strategy
    jobs._STRATEGY_CACHE["T"] = _strategy()
    return TestClient(server.app)


def test_full_run_lifecycle_over_http(client):
    # "inf" as a STRING: JSON has no Infinity literal and httpx's request encoder forbids
    # float('inf') (allow_nan=False); SimRunConfig.from_dict decodes "inf" -> float('inf')
    # (hold-past-stop), so the string form is the wire-correct spelling of the same value.
    body = {"strategy_name": "T", "source": "csv", "csv_path": FIXTURE,
            "n_paths": 30, "chunk_size": 15, "bar_size": "1m", "lookback_days": 10,
            "sl_multipliers": [2.0, "inf"], "equity": 100000}
    r = client.post("/api/sim/run", json=body)
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    deadline = time.time() + 120
    status = None
    while time.time() < deadline:
        status = client.get(f"/api/sim/status/{job_id}").json()
        if status["state"] in ("done", "error", "cancelled"):
            break
        time.sleep(0.05)
    assert status["state"] == "done", status
    result = client.get(f"/api/sim/result/{job_id}").json()
    assert len(result["cells"]) == 2
    for cell in result["cells"]:
        assert cell["stats"]["n"] == 30
        assert all(np.isfinite(cell["hist"]["edges"]))
    assert client.get("/api/sim/status/unknown").status_code == 404


def test_result_200_when_entry_window_starts_after_first_bar(client):
    """Regression: GET /api/sim/result 500'd with 'ValueError: Out of range float values
    are not JSON compliant' whenever a fan column has no finite marks. An entry window
    starting at 09:40 maps to bar index 9 on 1m bars (window_minutes), so minutes 0-8 are
    all-NaN across entered paths and used to reach the JSON layer as NaN."""
    from spx_trade_desk.sim import jobs

    from tests.test_sim_engine import _strategy
    jobs._STRATEGY_CACHE["T"] = _strategy(window=("09:40", "10:00"))
    body = {"strategy_name": "T", "source": "csv", "csv_path": FIXTURE,
            "n_paths": 12, "chunk_size": 12, "bar_size": "1m", "lookback_days": 10,
            "equity": 100000}
    r = client.post("/api/sim/run", json=body)
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    deadline = time.time() + 120
    status = None
    while time.time() < deadline:
        status = client.get(f"/api/sim/status/{job_id}").json()
        if status["state"] in ("done", "error", "cancelled"):
            break
        time.sleep(0.05)
    assert status["state"] == "done", status
    resp = client.get(f"/api/sim/result/{job_id}")
    assert resp.status_code == 200, resp.text
    cell = resp.json()["cells"][0]
    assert cell["stats"]["entered"] > 0            # a real fan, not the empty-mtm degenerate
    assert cell["fan"]["minutes"][0] == 0
    assert cell["fan"]["q50"][0] is None           # no entry possible at bar 0 -> gap


def test_run_rejects_bad_config(client):
    r = client.post("/api/sim/run", json={"strategy_name": "", "n_paths": 10})
    assert r.status_code == 400
    assert "strategy_name" in r.json()["detail"]


def test_run_second_job_while_busy_conflicts(client, monkeypatch):
    """Deterministic: hold the first job open on an Event so the second POST must 409."""
    import threading

    from spx_trade_desk.sim import jobs

    release, started = threading.Event(), threading.Event()

    def slow_pipeline(cfg, bars, cb, spot0, cancel_check=None, state=None):
        started.set()
        release.wait(10)
        return {"meta": {"strategy": cfg.strategy_name}, "cells": []}

    monkeypatch.setattr(jobs, "execute_pipeline", slow_pipeline)
    body = {"strategy_name": "T", "source": "csv", "csv_path": FIXTURE, "n_paths": 10}
    r1 = client.post("/api/sim/run", json=body)
    assert r1.status_code == 200
    assert started.wait(5), "background job never started"
    r2 = client.post("/api/sim/run", json=body)
    assert r2.status_code == 409
    assert client.post(f"/api/sim/cancel/{r1.json()['job_id']}").json()["cancelled"] is True
    release.set()
    deadline = time.time() + 15
    while time.time() < deadline:
        if client.get(f"/api/sim/status/{r1.json()['job_id']}").json()["state"] in ("done", "cancelled"):
            break
        time.sleep(0.05)
    assert client.get(f"/api/sim/status/{r1.json()['job_id']}").json()["state"] in ("done", "cancelled")


def test_result_returns_409_when_job_not_finished(client, monkeypatch):
    """GET /api/sim/result/{id} must 409 (not 200/500) while the job is still running."""
    import threading

    from spx_trade_desk.sim import jobs

    release, started = threading.Event(), threading.Event()

    def slow_pipeline(cfg, bars, cb, spot0, cancel_check=None, state=None):
        started.set()
        release.wait(10)
        return {"meta": {"strategy": cfg.strategy_name}, "cells": []}

    monkeypatch.setattr(jobs, "execute_pipeline", slow_pipeline)
    body = {"strategy_name": "T", "source": "csv", "csv_path": FIXTURE, "n_paths": 10}
    r = client.post("/api/sim/run", json=body)
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    assert started.wait(5), "background job never started"
    rr = client.get(f"/api/sim/result/{job_id}")
    assert rr.status_code == 409
    assert rr.json()["detail"] == "job not finished"
    assert client.post(f"/api/sim/cancel/{job_id}").json()["cancelled"] is True
    release.set()
    deadline = time.time() + 15
    while time.time() < deadline:
        if client.get(f"/api/sim/status/{job_id}").json()["state"] in ("done", "cancelled"):
            break
        time.sleep(0.05)


def test_pricing_endpoint_reports_the_cold_tier(client):
    r = client.get("/api/sim/pricing")
    assert r.status_code == 200
    body = r.json()
    assert body["tier"] == "cold" and body["library"] is None
    assert any(w.startswith("pricing: tier cold") for w in body["warnings"])
    assert client.get("/api/sim/pricing", params={"tier": "regime"}).status_code == 400


def test_pricing_rebuild_builds_from_the_library(client, monkeypatch, tmp_path):
    from spx_trade_desk.sim import library
    from tests.sim_chain_fixture import write_day
    write_day(tmp_path, "20300304", seed=1, step_min=30)
    monkeypatch.setattr(library, "CHAIN_LIBRARY_DIR", tmp_path)
    monkeypatch.setattr(library, "MODEL_PATH", tmp_path / "pricing_model.json")
    r = client.post("/api/sim/pricing/rebuild")
    assert r.status_code == 200 and r.json()["library"]["days"] == 1
    assert (tmp_path / "pricing_model.json").exists()


def test_pricing_rebuild_failure_is_a_500(client, monkeypatch):
    from spx_trade_desk.sim import library

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(library, "build_and_write", boom)
    r = client.post("/api/sim/pricing/rebuild")
    assert r.status_code == 500 and "rebuild failed" in r.json()["detail"]
