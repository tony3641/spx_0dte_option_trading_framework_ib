"""Tests for chain_manager stream-recovery helpers (pure functions)."""

from spx_trade_desk.market.chain_manager import chain_stream_status_line, unknown_retry_due

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
