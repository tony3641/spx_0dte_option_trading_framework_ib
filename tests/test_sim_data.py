# tests/test_sim_data.py
import os
import numpy as np
import pytest

from spx_trade_desk.sim.sim_config import SimRunConfig
from spx_trade_desk.sim.sim_data import BarSeries, load_bars, parse_csv

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "SPX_1min_10d.csv")


def test_parse_csv_fixture():
    bs = parse_csv(FIXTURE, 60)
    assert bs.source == "csv" and bs.bar_seconds == 60
    assert len(bs.closes) == 3900
    # 10 consecutive weekdays x 390 bars; minute_of_day repeats 09:31..16:00
    mods = bs.minute_of_day[:390]
    assert mods[0] == 9 * 60 + 31 and mods[-1] == 16 * 60
    assert np.isfinite(bs.closes).all() and (bs.closes > 0).all()
    # bars outside RTH are absent by construction, but guard the filter anyway
    assert (bs.minute_of_day >= 570).all() and (bs.minute_of_day <= 960).all()


def test_load_bars_csv_layer():
    cfg = SimRunConfig(strategy_name="Main", source="csv", csv_path=FIXTURE,
                       bar_size="1m", lookback_days=10)
    bs = load_bars(cfg)
    assert bs.source == "csv" and len(bs.closes) == 3900


def test_load_bars_preloaded_bypass():
    cfg = SimRunConfig(strategy_name="Main", source="csv", csv_path=FIXTURE)
    pre = BarSeries(closes=np.array([1.0, 2.0]), minute_of_day=np.array([571, 572]),
                    bar_seconds=60, source="csv")
    assert load_bars(cfg, bars=pre) is pre


def test_load_bars_missing_csv_raises():
    cfg = SimRunConfig(strategy_name="Main", source="csv", csv_path="Z:/nope.csv")
    with pytest.raises(FileNotFoundError):
        load_bars(cfg)
