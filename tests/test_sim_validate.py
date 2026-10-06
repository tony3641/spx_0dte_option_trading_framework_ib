# tests/test_sim_validate.py
"""Validation harness (SP2 spec section 4.2). Invented days only."""
import json

from spx_trade_desk.sim import validate
from spx_trade_desk.sim.pricing_tables import TAU_LABELS
from tests.sim_chain_fixture import VIX1D_PREV, truth_tables, write_day

DAY = "20300304"
PREV = {DAY: VIX1D_PREV, "20300305": VIX1D_PREV}
REPORT_KEYS = {"v", "pricer", "tier", "resolved_tiers", "cold_provisional", "days", "forecast",
               "real_atm", "passed_buckets", "scored_buckets", "generated", "notes"}


def _run(tmp_path, cold, tier="cold", **kw):
    p = write_day(tmp_path, DAY, seed=1)
    return validate.run_harness([p], tier, root=tmp_path, cold=cold, overrides=PREV, daily={}, **kw)


def test_exact_model_passes(tmp_path):
    rep = _run(tmp_path, truth_tables())
    assert rep["scored_buckets"] >= 3 and rep["passed_buckets"] == rep["scored_buckets"]
    assert all(r["pass"] in (True, None) for r in rep["real_atm"])
    assert rep["resolved_tiers"] == {DAY: "cold"} and rep["days"] == [DAY]


def test_wrong_level_fails_the_forecast_but_not_the_real_atm_score(tmp_path):
    t = truth_tables()
    t.atm_vix1d_ratio *= 1.6
    rep = _run(tmp_path, t)
    assert rep["passed_buckets"] == 0 < rep["scored_buckets"]
    assert all(r["pass"] in (True, None) for r in rep["real_atm"])


def test_leave_one_out_excludes_the_scored_day(tmp_path, monkeypatch):
    p1 = write_day(tmp_path, DAY, seed=1, step_min=30)
    write_day(tmp_path, "20300305", seed=2, step_min=30)
    seen = []
    real = validate.build_model_dict
    monkeypatch.setattr(validate, "build_model_dict",
                        lambda days, prev, scores=None: seen.append([d.day for d in days])
                        or real(days, prev, scores))
    rep = validate.run_harness([p1], "thin", root=tmp_path, cold=truth_tables(),
                               overrides=PREV, daily={})
    assert seen == [["20300305"]] and rep["resolved_tiers"] == {DAY: "thin"}


def test_missing_vix1d_skips_the_day_with_a_note(tmp_path):
    p = write_day(tmp_path, DAY, seed=1, step_min=30)
    rep = validate.run_harness([p], "cold", root=tmp_path, cold=truth_tables(), overrides={}, daily={})
    assert rep["days"] == [] and "no VIX1D prior close" in rep["notes"][0]


def test_report_json_schema_and_output(tmp_path):
    rep = _run(tmp_path, truth_tables())
    assert set(rep) == REPORT_KEYS
    assert [r["bucket"] for r in rep["forecast"]] == list(TAU_LABELS)
    assert set(rep["forecast"][0]) == {"bucket", "bar", "n", "credit_med_abs_err",
                                       "strike_within_5", "hs_med_abs_err", "pass"}
    out = validate.write_report(rep, tmp_path / "r.json")
    assert json.loads(out.read_text())["days"] == [DAY]
    assert "passed" in validate.format_report(rep)


def test_store_scores_records_the_resolved_tier(tmp_path):
    rep = _run(tmp_path, truth_tables())
    path = tmp_path / "pricing_model.json"
    path.write_text(json.dumps({"v": 1, "scores": {}}))
    validate.store_scores(rep, path)
    s = json.loads(path.read_text())["scores"]["cold"]
    assert s["passed"] == rep["passed_buckets"] and s["scored"] == rep["scored_buckets"]


def test_legacy_pricer_scores_the_same_records(tmp_path):
    rep = _run(tmp_path, truth_tables(), pricer_kind="legacy")
    assert rep["pricer"] == "legacy" and rep["resolved_tiers"] == {DAY: "legacy"}
    assert sum(r["n"] for r in rep["forecast"]) > 0
