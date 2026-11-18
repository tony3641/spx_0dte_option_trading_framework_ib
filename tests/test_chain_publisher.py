"""Publisher: the quote book becomes GEX + chain payload + state every cycle."""
import json
import os
import time
from datetime import datetime

import pytest

from spx_trade_desk.market import chain_publisher as pub
from spx_trade_desk.market.chain_publisher import publish_chain, tte_years
from spx_trade_desk.market.chain_recorder import ChainRecorder
from spx_trade_desk.market.gex import OptionData
from spx_trade_desk.market.hours import ET
from spx_trade_desk.market.quote_book import QuoteBook


class Sink:
    def __init__(self):
        self.msgs = []

    async def __call__(self, msg):
        self.msgs.append(msg)


def _state(app_state, *options):
    st = app_state
    st.expiration, st.spx_price = "20261005", 7700.0
    st.quote_book = QuoteBook()
    st.quote_book.reset("20261005")
    st.quote_book.update(list(options), "poll", now=0.0)
    return st


def test_tte_years_same_day_and_future():
    now = datetime(2026, 10, 5, 15, 0, tzinfo=ET)
    assert tte_years("20261005", now) == pytest.approx(60 / (390 * 252))
    assert tte_years("20261007", now) == pytest.approx(2 / 252)


@pytest.mark.asyncio
async def test_publish_sets_state_and_broadcasts(app_state):
    st = _state(app_state,
                OptionData(7690, "P", gamma=0.01, open_interest=100, bid=1.0, ask=1.2, delta=-0.3),
                OptionData(7710, "C", gamma=0.01, open_interest=80, bid=1.1, ask=1.3, delta=0.3))
    st.chain_fetching = True
    sink = Sink()
    assert await publish_chain(st, sink, now_mono=30.0)
    assert len(st.chain_data) == 2 and st.latest_gex is not None
    assert st.chain_quotes_cache["scope"] == "full"
    row = next(r for r in st.chain_quotes_cache["strikes"] if r["strike"] == 7690)
    assert row["put_age_s"] == 30.0
    assert [m["type"] for m in sink.msgs] == ["gex", "chain_quotes", "chain_progress"]
    assert sink.msgs[2]["data"]["phase"] == "done" and not st.chain_fetching


@pytest.mark.asyncio
async def test_second_publish_does_not_repeat_chain_progress(app_state):
    st = _state(app_state, OptionData(7690, "P", bid=1.0))
    sink = Sink()
    await publish_chain(st, sink, now_mono=1.0)
    assert [m["type"] for m in sink.msgs] == ["gex", "chain_quotes"]


@pytest.mark.asyncio
async def test_oi_fallback_applies_to_the_gex_copy_only(app_state):
    st = _state(app_state, OptionData(7690, "P", gamma=0.01, open_interest=0, volume=50, bid=1.0))
    await publish_chain(st, Sink(), now_mono=0.0)
    assert st.gex_result.put_oi_by_strike[7690.0] == 50
    assert st.chain_data[0].open_interest == 0
    assert st.quote_book.options()[0].open_interest == 0


@pytest.mark.asyncio
async def test_publish_with_an_empty_book_does_nothing(app_state):
    st = _state(app_state)
    sink = Sink()
    assert not await publish_chain(st, sink, now_mono=0.0)
    assert sink.msgs == []


@pytest.mark.asyncio
async def test_publish_stamps_the_cache_with_its_build_time(app_state):
    st = _state(app_state, OptionData(7690, "P", bid=1.0))
    await publish_chain(st, Sink(), now_mono=123.0)
    assert st.chain_quotes_cache["built_mono"] == pytest.approx(time.monotonic(), abs=5.0)


@pytest.mark.asyncio
async def test_publish_tick_touches_the_heartbeat_even_when_no_record_is_due(
        app_state, tmp_path, monkeypatch):
    monkeypatch.setattr(pub, "now_et", lambda: datetime(2026, 10, 5, 9, 20, tzinfo=ET))
    st = _state(app_state, OptionData(7690, "P", bid=1.0))
    rec = ChainRecorder(tmp_path)
    rec.touch_heartbeat()
    os.utime(rec.heartbeat, (1.0, 1.0))
    assert await pub.publish_tick(st, Sink(), rec, now_mono=1.0)
    assert rec.heartbeat.stat().st_mtime > 1_000_000
    assert not (tmp_path / "20261005.jsonl.gz").exists()        # 09:20: nothing due


@pytest.mark.asyncio
async def test_publish_tick_does_not_touch_the_heartbeat_when_the_publish_failed(
        app_state, tmp_path):
    st = _state(app_state)                                      # empty book -> publish returns False
    rec = ChainRecorder(tmp_path)
    assert not await pub.publish_tick(st, Sink(), rec, now_mono=1.0)
    assert not rec.heartbeat.exists()


@pytest.mark.asyncio
async def test_publish_tick_without_a_recorder_just_publishes(app_state):
    st = _state(app_state, OptionData(7690, "P", bid=1.0))
    assert await pub.publish_tick(st, Sink(), None, now_mono=1.0)


def _chain(iv=0.15):
    """Invented chain: strikes 7600..7800; dealers' OI sits on calls above 7700 and puts below it."""
    opts = []
    for k in range(7600, 7810, 10):
        for right in ("C", "P"):
            oi = 1000 if (right == "C" and k > 7700) or (right == "P" and k < 7700) else 0
            bid, ask = (9.8, 10.2) if k == 7700 else (1.0, 1.2)
            opts.append(OptionData(k, right, implied_vol=iv, open_interest=oi, bid=bid, ask=ask))
    return opts


def _chain_200_strikes():
    """Invented chain with 200 strikes per right, for the payload size check."""
    opts = []
    for k in range(7000, 9000, 10):
        for right in ("C", "P"):
            oi = 1000 if (right == "C" and k > 7700) or (right == "P" and k < 7700) else 0
            opts.append(OptionData(k, right, implied_vol=0.15, open_interest=oi, bid=1.0, ask=1.2))
    return opts


@pytest.mark.asyncio
async def test_publish_adds_the_dealer_block_and_zero_gamma_replaces_the_flip(app_state, monkeypatch):
    monkeypatch.setattr(pub, "now_et", lambda: datetime(2026, 10, 5, 10, 0, tzinfo=ET))
    st, sink = _state(app_state, *_chain()), Sink()
    await publish_chain(st, sink, now_mono=0.0)
    gex = sink.msgs[0]["data"]
    d = gex["dealer"]
    assert {"model", "regime", "gamma_at_spot", "zero_gamma", "zero_gamma_all", "zero_gamma_distance",
            "profile", "nodes", "voids", "em", "charm", "vanna"} <= set(d)
    assert d["model"] == "oi_assumed" and gex["gamma_flip"] == d["zero_gamma"]
    assert gex["gamma_flip_method"] == "zero_gamma"
    assert "gamma_flip_cumulative" in gex
    assert st.latest_gex["dealer"] == d and st.latest_dealer == d
    assert st.chain_quotes_cache["gamma_flip"] == gex["gamma_flip"]
    assert d["em"]["live"] == pytest.approx(20.0) and d["em"]["anchor_time"] == "10:00"
    assert (gex["em_low"], gex["em_high"]) == (7680.0, 7720.0)


@pytest.mark.asyncio
async def test_without_any_iv_there_is_no_dealer_block_and_the_flip_is_cumulative(app_state):
    st = _state(app_state, OptionData(7690, "P", gamma=0.01, open_interest=100, bid=1.0, ask=1.2))
    sink = Sink()
    await publish_chain(st, sink, now_mono=0.0)
    assert sink.msgs[0]["data"]["dealer"] is None
    assert sink.msgs[0]["data"]["gamma_flip_method"] == "cumulative"


@pytest.mark.asyncio
async def test_the_level_history_records_the_em_band_and_the_nearest_pins(app_state, monkeypatch):
    monkeypatch.setattr(pub, "now_et", lambda: datetime(2026, 10, 5, 10, 0, tzinfo=ET))
    st = _state(app_state, *_chain())
    await publish_chain(st, Sink(), now_mono=0.0)
    point = st.level_history.points()[-1]
    assert {"em_high", "em_low", "node_r1", "node_s1"} <= set(point)
    assert point["em_high"] == 7720.0 and point["em_low"] == 7680.0


@pytest.mark.asyncio
async def test_the_dealer_block_stays_compact(app_state, monkeypatch):      # sent to every browser every 10 s
    monkeypatch.setattr(pub, "now_et", lambda: datetime(2026, 10, 5, 10, 0, tzinfo=ET))
    st = _state(app_state, *_chain_200_strikes())
    await publish_chain(st, Sink(), now_mono=0.0)
    assert st.latest_dealer is not None
    assert len(json.dumps(st.latest_dealer)) < 6000
