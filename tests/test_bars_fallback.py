"""market/bars.py fetch_historical_bars: the chain poller's no-spot fallback seeds the price series."""
from datetime import datetime
from types import SimpleNamespace

import pytest

from spx_trade_desk.core.app_state import create_app_state
from spx_trade_desk.market import bars
from spx_trade_desk.market.hours import ET
from spx_trade_desk.market.price_bars import snapshot_payload
from tests.conftest import MockIBClient

DAY = "2099-01-05"       # invented date


def _bar(hhmm, close):
    h, m = map(int, hhmm.split(":"))
    return SimpleNamespace(date=datetime(2099, 1, 5, h, m, tzinfo=ET), open=close, high=close, low=close, close=close)


@pytest.mark.asyncio
async def test_fallback_fetch_sets_the_session_date_so_snapshots_and_updates_agree():
    ib, st = MockIBClient(), create_app_state()
    st.spx_contract = SimpleNamespace(symbol="SPX")

    async def one_day(contract, **kw):
        return [_bar("09:30", 100.0), _bar("09:31", 101.0)]

    ib.req_historical_bars = one_day
    await bars.fetch_historical_bars(ib, st)
    assert st.price_session_date == DAY and len(st.price_history) == 2
    assert snapshot_payload(st, False, DAY)["session_date"] == DAY
