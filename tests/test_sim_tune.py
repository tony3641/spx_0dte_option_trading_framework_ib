# tests/test_sim_tune.py
"""Tests for the strategy-tuning variant runner (tune.py)."""
import copy
import csv
import json
import os

import pytest

from spx_trade_desk.sim import jobs
from spx_trade_desk.sim import tune
from spx_trade_desk.strategy.models import Condition, Strategy

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "SPX_1min_10d.csv")
CONFIG_BYTES = os.path.join(os.path.dirname(__file__), "..", "config", "strategies.json")


def _strategy(name: str = "T") -> Strategy:
    """Minimal simulable bull_put (same shape as tests/test_sim_engine.py's helper)."""
    return Strategy.from_dict({
        "name": name,
        "direction": "bull_put",
        "conditions": [
            {"kind": "short_delta", "params": {"min": 0.30, "max": 0.45}},
            {"kind": "spread_width", "params": {"min": 40, "max": 65}},
            {"kind": "credit", "params": {"min": 3.0}},
            {"kind": "entry_window", "params": {"start": "09:35", "end": "10:00"}},
        ],
        "exit_rules": {"stop_loss": {"multiplier": 6.0}},
        "budget": None,
    })


def _spec(**kw):
    d = dict(
        slug="test-tune",
        strategy="T",
        dataset={"source": "csv", "csv_path": FIXTURE, "bar_size": "1m", "spot0": 7718.36,
                 "lookback_days": 10},
        run={"n_paths": 40, "chunk_size": 20, "bootstrap_seqs": 50, "bootstrap_len": 20},
        stress={},
        variants=[
            {"name": "credit_floor", "knobs": {"credit.min": 99.0}},   # enters nothing
            {"name": "delta_band", "knobs": {"short_delta.min": 0.10,
                                             "short_delta.max": 0.45}},
        ],
    )
    d.update(kw)
    return d


_QUIET = lambda *a, **k: None  # noqa: E731


@pytest.fixture(autouse=True)
def _clean():
    jobs.reset_registry()
    jobs._STRATEGY_CACHE.clear()
    yield
    jobs.reset_registry()
    jobs._STRATEGY_CACHE.clear()


# ---------------------------------------------------------------- knob application

def test_apply_knobs_whitelist_and_unknown_raises():
    s = _strategy()
    tune.apply_knobs(s, {"short_delta.min": 0.05, "entry_window.start": "9:45",
                             "exit.stop_multiplier": 4.0, "take_profit.pct": 0.5})
    by_kind = {c.kind: c for c in s.conditions}
    assert by_kind["short_delta"].params["min"] == 0.05
    assert by_kind["entry_window"].params["start"] == "09:45"   # normalized HH:MM
    assert s.exit_rules.stop_loss.multiplier == 4.0
    assert s.exit_rules.take_profit.mode == "pct_credit"
    assert s.exit_rules.take_profit.value == 0.5
    # None unsets: TP cleared, credit.max param removed (extract_conditions inf bound)
    tune.apply_knobs(s, {"take_profit.pct": None, "credit.max": None,
                             "volatility.vix_enabled": True, "volatility.vix_op": "below",
                             "volatility.vix_value": 13})
    assert s.exit_rules.take_profit is None
    vol = {c.kind: c for c in s.conditions}["volatility"]
    assert vol.params["vix_enabled"] is True and vol.params["vix_op"] == "below"
    with pytest.raises(ValueError, match="allowed knobs"):
        tune.apply_knobs(_strategy(), {"trend.period": 14})
    with pytest.raises(ValueError, match="vix_op"):
        tune.apply_knobs(_strategy(), {"volatility.vix_op": "sideways"})


def test_apply_knobs_does_not_mutate_the_original():
    s = _strategy()
    orig = copy.deepcopy(s)
    tune.apply_knobs(copy.deepcopy(s), {"short_delta.max": 0.50})
    assert s.to_dict() == orig.to_dict()


# ---------------------------------------------------------------- fail-fast guards

def test_unsupported_live_strategy_fails_before_compute(tmp_path):
    trendy = _strategy("TRENDY")
    trendy.conditions.append(Condition(kind="trend",
                                       params={"indicator": "rsi", "period": 14}))
    with pytest.raises(ValueError, match="trend"):
        tune.run_experiment(_spec(strategy="TRENDY"), str(tmp_path / "out"),
                                strategies={"TRENDY": trendy}, log=_QUIET)
    assert not os.path.exists(tmp_path / "out")   # rejected before any artifact/IO


def test_unknown_strategy_and_bad_spec_fail_clean(tmp_path):
    with pytest.raises(ValueError, match="unknown strategy"):
        tune.run_experiment(_spec(strategy="Nope"), str(tmp_path),
                                strategies={"T": _strategy()}, log=_QUIET)
    with pytest.raises(ValueError, match="spec key"):
        tune.run_experiment(_spec(bogus_key=1), str(tmp_path),
                                strategies={"T": _strategy()}, log=_QUIET)
    with pytest.raises(ValueError, match="reserved"):
        spec = _spec(variants=[{"name": "baseline", "knobs": {}}])
        tune.run_experiment(spec, str(tmp_path),
                                strategies={"T": _strategy()}, log=_QUIET)


# ---------------------------------------------------------------- pipeline runs

def test_runner_crn_identical_market_paths(tmp_path):
    """Same seed + identical cfg => byte-identical spot paths across variants."""
    results = tune.run_experiment(_spec(), str(tmp_path),
                                      strategies={"T": _strategy()}, log=_QUIET)
    assert results["meta"]["crn_ok"] is True
    assert [p["name"] for p in results["variants"]] == \
        ["baseline", "credit_floor", "delta_band"]


def test_runner_is_deterministic_across_invocations(tmp_path):
    out1, out2 = tmp_path / "a", tmp_path / "b"
    for out in (out1, out2):
        tune.run_experiment(_spec(), str(out), strategies={"T": _strategy()},
                                log=_QUIET)
    a = (out1 / "results.csv").read_bytes()
    b = (out2 / "results.csv").read_bytes()
    assert a == b


def test_runner_injects_overridden_strategy_without_touching_config_file(tmp_path):
    with open(CONFIG_BYTES, "rb") as f:
        before = f.read()
    results = tune.run_experiment(_spec(), str(tmp_path),
                                      strategies={"T": _strategy()}, log=_QUIET)
    with open(CONFIG_BYTES, "rb") as f:
        assert f.read() == before                     # live config untouched
    by_name = {p["name"]: p for p in results["variants"]}
    base, floored = by_name["baseline"]["cell"], by_name["credit_floor"]["cell"]
    assert base["stats"]["entered"] > 0               # the strategy trades as-is...
    assert floored["stats"]["never_entered_pct"] == 1.0   # ...and the knob took effect


def test_runner_writes_csv_and_json_schemas(tmp_path):
    tune.run_experiment(_spec(), str(tmp_path), strategies={"T": _strategy()},
                            log=_QUIET)
    with open(tmp_path / "results.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
        assert rows[0].keys() == dict.fromkeys(tune.CSV_COLUMNS).keys()
    assert len(rows) == 3                             # baseline + 2 variants, 1 seed
    data = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    assert set(data) == {"meta", "variants"}
    m = data["meta"]
    assert m["strategy"] == "T" and m["crn_ok"] is True and m["seeds"] == [42]
    for p in data["variants"]:
        assert set(p) == {"name", "seed", "knobs", "cell", "run_meta"}
        assert set(p["cell"]) >= {"stats", "breakdown", "dd", "ruin_prob", "fan"}


def test_runner_multi_seed_rows(tmp_path):
    results = tune.run_experiment(_spec(), str(tmp_path), seeds=[42, 43],
                                      strategies={"T": _strategy()}, log=_QUIET)
    assert results["meta"]["seeds"] == [42, 43]
    names = [p["name"] for p in results["variants"]]
    assert names == ["baseline", "credit_floor", "delta_band"] * 2
    with open(tmp_path / "results.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert [r["seed"] for r in rows] == ["42", "42", "42", "43", "43", "43"]


def test_main_cli_smoke_end_to_end(tmp_path, monkeypatch):
    # Hermetic store: main() reads the live store, so patch it (run_experiment
    # imports load_strategies at call time).
    monkeypatch.setattr("spx_trade_desk.strategy.store.load_strategies",
                        lambda path=None: {"T": _strategy()})
    spec_path = tmp_path / "variants.json"
    spec_path.write_text(json.dumps(_spec()), encoding="utf-8")
    rc = tune.main(["--spec", str(spec_path), "--out", str(tmp_path / "out"),
                        "--smoke", "--n-paths", "100", "--seed", "42"])
    assert rc == 0
    assert (tmp_path / "out" / "results.csv").exists()
    data = json.loads((tmp_path / "out" / "results.json").read_text(encoding="utf-8"))
    assert data["meta"]["smoke"] is True
    assert data["meta"]["run"]["n_paths"] == 100   # explicit --n-paths beats smoke default
