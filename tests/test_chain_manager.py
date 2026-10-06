"""Tests for chain_manager stream-recovery helpers (pure functions)."""

import pytest

from spx_trade_desk.market.chain_manager import chain_stream_status_line, unknown_retry_due
from tests.conftest import MockContract, MockIBClient

NOW = 1000.0
COOLDOWN = 120.0


class TestUnknownRetryDue:
    """Blacklisted qualification keys retry once the cooldown elapses."""

    def test_empty_blacklist_yields_nothing(self):
        assert unknown_retry_due({}, NOW, COOLDOWN) == set()

    def test_recent_failure_stays_blacklisted(self):
        key = (6000.0, "C")
        assert unknown_retry_due({key: NOW - 1.0}, NOW, COOLDOWN) == set()

    def test_failure_at_exact_cooldown_is_retryable(self):
        key = (6000.0, "C")
        assert unknown_retry_due({key: NOW - COOLDOWN}, NOW, COOLDOWN) == {key}

    def test_old_failure_is_retryable(self):
        key = (6000.0, "C")
        assert unknown_retry_due({key: NOW - COOLDOWN - 60.0}, NOW, COOLDOWN) == {key}

    def test_mixed_freshness_returns_only_stale_keys(self):
        fresh = (6000.0, "C")
        stale = (6005.0, "P")
        unknown = {fresh: NOW - 5.0, stale: NOW - COOLDOWN - 5.0}
        assert unknown_retry_due(unknown, NOW, COOLDOWN) == {stale}

    def test_default_cooldown_comes_from_config(self):
        from spx_trade_desk.core.config import CHAIN_STREAM_UNKNOWN_RETRY_SECS
        key = (6000.0, "C")
        assert unknown_retry_due({key: NOW - CHAIN_STREAM_UNKNOWN_RETRY_SECS}, NOW) == {key}


class TestChainStreamStatusLine:
    """The 10s status log explains itself when quotes are not flowing."""

    def test_quotes_present_log_unchanged(self):
        assert chain_stream_status_line(96, 40, 96) == "Chain stream ticks: 96 contracts, quotes_present=40, active_subs=96"

    def test_zero_quotes_adds_recovery_hint(self):
        line = chain_stream_status_line(96, 0, 96)
        assert "quotes_present=0" in line
        assert "2157" in line  # sec-def farm outage signature

    def test_zero_quotes_with_no_subs_gets_no_hint(self):
        assert chain_stream_status_line(0, 0, 0) == "Chain stream ticks: 0 contracts, quotes_present=0, active_subs=0"



from spx_trade_desk.market.chain_manager import build_chain_quotes
from spx_trade_desk.market.gex import OptionData


class TestBuildChainQuotesAges:
    def test_adds_per_side_age_and_max_age(self):
        opts = [OptionData(7700, "P", bid=1.0), OptionData(7700, "C", bid=2.0)]
        q = build_chain_quotes(opts, 7700.0, ages={(7700.0, "P"): 12.34, (7700.0, "C"): 0.0},
                               max_age_s=180)
        row = q["strikes"][0]
        assert row["put_age_s"] == 12.3 and row["call_age_s"] == 0.0
        assert q["max_age_s"] == 180

    def test_without_ages_has_no_age_fields(self):
        q = build_chain_quotes([OptionData(7700, "P", bid=1.0)], 7700.0)
        assert "put_age_s" not in q["strikes"][0]
        assert q["max_age_s"] is None


from spx_trade_desk.ib.client import TickStream
from spx_trade_desk.market.chain_manager import _collect_stream_quotes


class TestCollectStreamQuotes:
    def _stream(self, req_id, ticked, bid=None):
        s = TickStream(req_id, contract=None)
        s.bid = bid
        if ticked:
            s._mark(has_quote=bid is not None)
        return s

    def test_collect_stream_quotes_skips_streams_without_ticks(self):
        tickers = {(7700.0, "P"): self._stream(1, True, bid=1.25),
                   (7705.0, "P"): self._stream(2, False)}
        ticks, live, book = _collect_stream_quotes(tickers, {(7705.0, "P"): 300})
        assert [t["strike"] for t in ticks] == [7700.0, 7705.0]
        assert [o.strike for o in live] == [7700.0, 7705.0]
        assert [(o.strike, o.bid) for o in book] == [(7700.0, 1.25)]
        assert live[1].open_interest == 300            # OI falls back to the last chain value


class TestCollectStreamQuotesFreshness:
    """A stream row reaches the book only when its stream ticked since the last pass."""

    def _ticked(self, req_id, bid, at):
        s = TickStream(req_id, contract=None)
        s.bid = bid
        s._mark(has_quote=True)
        s.last_tick_mono = at
        return s

    def test_untick_ed_stream_key_is_not_restamped(self):
        a, b = self._ticked(1, 1.0, 100.0), self._ticked(2, 2.0, 100.0)
        tickers = {(7700.0, "P"): a, (7705.0, "P"): b}
        seen = {}
        _, _, book = _collect_stream_quotes(tickers, {}, seen)
        assert [o.strike for o in book] == [7700.0, 7705.0]
        _, live, book = _collect_stream_quotes(tickers, {}, seen)        # silent farm
        assert book == [] and len(live) == 2                              # tick payload unchanged
        a.last_tick_mono = 101.0                                          # only A ticks
        _, _, book = _collect_stream_quotes(tickers, {}, seen)
        assert [o.strike for o in book] == [7700.0]

    def test_without_a_seen_map_every_ticked_stream_is_collected(self):
        tickers = {(7700.0, "P"): self._ticked(1, 1.0, 100.0)}
        assert len(_collect_stream_quotes(tickers, {})[2]) == 1
        assert len(_collect_stream_quotes(tickers, {})[2]) == 1

    def test_a_resubscribed_key_is_collected_again(self):
        key = (7700.0, "P")
        seen = {}
        _collect_stream_quotes({key: self._ticked(1, 1.0, 100.0)}, {}, seen)
        _, _, book = _collect_stream_quotes({key: self._ticked(9, 1.1, 150.0)}, {}, seen)
        assert [o.bid for o in book] == [1.1]


from spx_trade_desk.market.chain_manager import STREAM_SUBSCRIBE_CHUNK, _subscribe_new_keys


def _qualified(keys):
    return {k: MockContract(conId=i + 1, symbol="SPX", secType="OPT", strike=k[0], right=k[1])
            for i, k in enumerate(keys)}


@pytest.mark.asyncio
async def test_subscribe_new_keys_takes_one_chunk_nearest_first(app_state):
    ib = MockIBClient()
    keys = [(7700.0 + 5 * i, "C") for i in range(45)]
    n = await _subscribe_new_keys(ib, app_state, _qualified(keys), keys)
    assert n == STREAM_SUBSCRIBE_CHUNK == 30
    assert list(app_state.chain_stream_tickers) == keys[:30]
    assert ib.line_budget.used("stream") == 30


@pytest.mark.asyncio
async def test_subscribe_new_keys_skips_unqualified_keys_and_stops_when_the_budget_is_full(app_state):
    ib = MockIBClient(line_shares={"fixed": 4, "order": 4, "poll": 12, "stream": 4})
    keys = [(7700.0 + 5 * i, "P") for i in range(10)]
    qualified = _qualified(keys[1:])                       # the nearest key could not be qualified
    n = await _subscribe_new_keys(ib, app_state, qualified, keys)
    assert n == 4 and (7700.0, "P") not in app_state.chain_stream_tickers
    assert ib.line_budget.used("stream") == 4


from spx_trade_desk.ib.client import TickStream
from spx_trade_desk.market.chain_manager import drop_failed_streams


def test_drop_failed_streams_removes_refused_streams_only(app_state):
    ok, bad = TickStream(1, None), TickStream(2, None)
    bad.failed_code = 101
    app_state.chain_stream_tickers = {(7700.0, "C"): ok, (7700.0, "P"): bad}
    app_state.chain_stream_contracts = {(7700.0, "C"): "c", (7700.0, "P"): "p"}
    assert drop_failed_streams(app_state) == 1
    assert set(app_state.chain_stream_tickers) == {(7700.0, "C")}
    assert set(app_state.chain_stream_contracts) == {(7700.0, "C")}
    assert drop_failed_streams(app_state) == 0
