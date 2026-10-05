"""Wing poller: everything in range that the stream does not hold."""
import pytest

from spx_trade_desk.market.chain_poller import poll_once, poll_targets
from spx_trade_desk.market.qualification import QualificationCache
from spx_trade_desk.market.quote_book import QuoteBook
from tests.conftest import MockIBClient

STRIKES = [7600.0 + 5 * i for i in range(41)]          # 7600 .. 7800


def test_poll_targets_cover_range_minus_streamed_nearest_first():
    streamed = {(7700.0, "C"), (7700.0, "P"), (7705.0, "P")}
    # 1% annual vol: +-8 daily sigma around 7700 is about +-38.8 pts -> 7665..7735
    t = poll_targets(STRIKES + [7702.5], spot=7700.0, annual_vol=0.01, streamed=streamed)
    assert t[:3] == [(7695.0, "C"), (7695.0, "P"), (7705.0, "C")]
    assert len(t) == 15 * 2 - 3
    assert (7702.5, "C") not in t
    assert all(7661 <= k <= 7739 for k, _ in t)


def _state(app_state):
    st = app_state
    st.expiration, st.trading_class = "20261005", "SPXW"
    st.spx_price, st.annual_vol = 7700.0, 0.01
    st.strikes = list(STRIKES)
    st.quote_book = QuoteBook()
    st.qual_cache = QualificationCache()
    return st


@pytest.mark.asyncio
async def test_poll_once_fills_book_in_poll_sized_batches(app_state):
    ib = MockIBClient(line_shares={"fixed": 4, "order": 4, "poll": 4, "stream": 0})
    st = _state(app_state)
    st.quote_book.reset("20261002")                     # yesterday's rows must go
    n = await poll_once(ib, st, now=lambda: 50.0)
    assert n == 30 and len(st.quote_book) == 30
    assert st.quote_book.expiry == "20261005"
    sizes = [c["count"] for c in ib.call_log if c["method"] == "fetch_snapshot"]
    assert max(sizes) <= 4 and sum(sizes) == 30
    assert set(st.quote_book.sources().values()) == {"poll"}
    assert ib.line_budget.used("poll") == 0


@pytest.mark.asyncio
async def test_poll_once_skips_streamed_contracts(app_state):
    ib = MockIBClient()
    st = _state(app_state)
    st.chain_stream_tickers = {(7700.0, "P"): object(), (7700.0, "C"): object()}
    n = await poll_once(ib, st, now=lambda: 0.0)
    assert n == 28
    assert (7700.0, "P") not in st.quote_book.sources()
