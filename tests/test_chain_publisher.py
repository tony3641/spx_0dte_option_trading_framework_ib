"""Publisher: the quote book becomes GEX + chain payload + state every cycle."""
from datetime import datetime

import pytest

from spx_trade_desk.market.chain_publisher import publish_chain, tte_years
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
