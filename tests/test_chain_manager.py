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


# -- chain_stream_loop: expiry roll during a qualification, zero stream share --------------------

import asyncio
import logging

from spx_trade_desk.market import chain_manager
from spx_trade_desk.market.chain_manager import chain_stream_loop
from spx_trade_desk.market.qualification import norm_key

_LOOP_INTERVAL = 0.0123            # distinctive: the fake sleep counts the loop's per-pass sleeps by it
OLD_EXP, NEW_EXP = "20261005", "20261006"


class _FakeRegistry:
    """Qualifies every key; ``flip_to`` rolls the state's expiry during the first qualification."""

    def __init__(self, state, flip_to=None):
        self.state = state
        self.flip_to = flip_to
        self.calls = []

    async def qualify_keys(self, ib, expiry, trading_class, keys, now):
        self.calls.append(expiry)
        out = {norm_key(*k): MockContract(conId=len(self.calls) * 1000 + i, symbol="SPX", secType="OPT",
                                          lastTradeDateOrContractMonth=expiry, strike=k[0], right=k[1],
                                          tradingClass=trading_class)
               for i, k in enumerate(keys)}
        if self.flip_to is not None and len(self.calls) == 1:
            await asyncio.sleep(0)               # a real suspension: the roll lands while we wait
            self.state.expiration = self.flip_to
        return out

    def unknown_count(self):
        return 0


async def _run_stream_loop(monkeypatch, ib, state, passes, sent=None, on_pass=None):
    """Run chain_stream_loop with no real delays until it has slept ``passes`` times, then stop it.

    ``sent`` collects every broadcast message. ``on_pass(n)`` runs inside the n-th per-pass sleep, i.e.
    after that pass's subscriptions and before its quotes are collected and broadcast. Passes 1..n-1 are
    always complete when the n-th sleep starts; pass n may or may not be.
    """
    real_sleep = asyncio.sleep
    done = asyncio.Event()
    slept = {"n": 0}

    async def fast_sleep(delay, *a, **k):
        if delay == _LOOP_INTERVAL:
            slept["n"] += 1
            if on_pass is not None:
                on_pass(slept["n"])
            if slept["n"] >= passes:
                done.set()
        await real_sleep(0)

    monkeypatch.setattr(chain_manager, "CHAIN_STREAM_UPDATE_INTERVAL", _LOOP_INTERVAL)
    monkeypatch.setattr(chain_manager, "is_cboe_options_open", lambda: True)
    monkeypatch.setattr(chain_manager.asyncio, "sleep", fast_sleep)

    async def broadcast(msg):
        if sent is not None:
            sent.append(msg)

    task = asyncio.create_task(chain_stream_loop(ib, state, broadcast))
    try:
        await asyncio.wait_for(done.wait(), timeout=5.0)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def _stream_state(app_state):
    st = app_state
    st.connected, st.expiration, st.trading_class = True, OLD_EXP, "SPXW"
    st.spx_price = 5200.0
    st.strikes = [5190.0, 5195.0, 5200.0, 5205.0, 5210.0]
    return st


@pytest.mark.asyncio
async def test_an_expiry_roll_during_qualification_subscribes_and_books_nothing_for_the_old_expiry(
        app_state, monkeypatch):
    st = _stream_state(app_state)
    st.contracts = _FakeRegistry(st, flip_to=NEW_EXP)
    ib = MockIBClient()
    subscribed, book_writes = [], []
    real_subscribe = ib.subscribe_tick_paced

    async def ticking_subscribe(contract, generic="", share="fixed"):
        stream = await real_subscribe(contract, generic, share)
        subscribed.append(contract.lastTradeDateOrContractMonth)
        stream.bid, stream.ask, stream.last = ((99.0, 99.1, 99.05)
                                               if contract.lastTradeDateOrContractMonth == OLD_EXP
                                               else (1.0, 1.1, 1.05))
        stream._mark(True)
        return stream

    ib.subscribe_tick_paced = ticking_subscribe
    real_update = st.quote_book.update

    def spy_update(options, source, now):
        book_writes.append((st.quote_book.expiry, [o.bid for o in options]))
        return real_update(options, source, now)

    monkeypatch.setattr(st.quote_book, "update", spy_update)
    await _run_stream_loop(monkeypatch, ib, st, passes=2)

    assert st.contracts.calls[0] == OLD_EXP and NEW_EXP in st.contracts.calls
    assert subscribed and set(subscribed) == {NEW_EXP}                  # nothing subscribed for the old expiry
    assert book_writes and all(exp == NEW_EXP for exp, _ in book_writes)
    assert all(b == 1.0 for _, bids in book_writes for b in bids)       # no old-expiry quote reached the book


@pytest.mark.asyncio
async def test_a_zero_stream_share_does_not_qualify_or_subscribe_or_warn_every_pass(
        app_state, monkeypatch, caplog):
    st = _stream_state(app_state)
    st.contracts = _FakeRegistry(st)
    ib = MockIBClient(line_shares={"fixed": 4, "order": 4, "poll": 12, "stream": 0})
    with caplog.at_level(logging.WARNING):
        await _run_stream_loop(monkeypatch, ib, st, passes=3)
    assert st.contracts.calls == []
    assert [c for c in ib.call_log if c["method"] == "subscribe_tick"] == []
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


# -- chain_stream_loop: only changed tick fields go out, no stream-scope chain_quotes --------------

import itertools
import time
import types

from spx_trade_desk.market.chain_manager import diff_ticks

TICK_FIELDS = {"bid", "ask", "bid_size", "ask_size", "last", "volume", "delta", "gamma", "iv"}


def test_diff_ticks_sends_everything_first_then_only_changes():
    last = {}
    t1 = [{"strike": 5000.0, "right": "C", "bid": 1.0, "ask": 1.2, "volume": 5}]
    assert diff_ticks(t1, last) == t1
    t2 = [{"strike": 5000.0, "right": "C", "bid": 1.0, "ask": 1.3, "volume": 5}]
    assert diff_ticks(t2, last) == [{"strike": 5000.0, "right": "C", "ask": 1.3}]
    assert diff_ticks(t2, last) == []
    assert last == {(5000.0, "C"): {"bid": 1.0, "ask": 1.3, "volume": 5}}


def test_diff_ticks_compares_with_the_last_sent_value_not_the_first():
    last = {}
    diff_ticks([{"strike": 5000.0, "right": "C", "bid": 1.0}], last)
    assert diff_ticks([{"strike": 5000.0, "right": "C", "bid": 2.0}], last) == [
        {"strike": 5000.0, "right": "C", "bid": 2.0}]
    assert diff_ticks([{"strike": 5000.0, "right": "C", "bid": 1.0}], last) == [
        {"strike": 5000.0, "right": "C", "bid": 1.0}]


def test_diff_ticks_keeps_each_strike_and_right_apart():
    last = {}
    both = [{"strike": 5000.0, "right": "C", "bid": 1.0}, {"strike": 5000.0, "right": "P", "bid": 1.0}]
    assert diff_ticks(both, last) == both
    assert diff_ticks([{"strike": 5000.0, "right": "C", "bid": 1.0},
                       {"strike": 5000.0, "right": "P", "bid": 1.5}], last) == [
        {"strike": 5000.0, "right": "P", "bid": 1.5}]


def test_diff_ticks_treats_a_value_turning_none_as_a_change():
    last = {}
    diff_ticks([{"strike": 5000.0, "right": "P", "bid": 1.0}], last)
    assert diff_ticks([{"strike": 5000.0, "right": "P", "bid": None}], last) == [
        {"strike": 5000.0, "right": "P", "bid": None}]


def _ticking_ib():
    """A mock IB whose stream subscriptions arrive already ticking (bid 1.0 / ask 1.2)."""
    ib = MockIBClient()
    real_subscribe = ib.subscribe_tick_paced

    async def ticking_subscribe(contract, generic="", share="fixed"):
        stream = await real_subscribe(contract, generic, share)
        stream.bid, stream.ask = 1.0, 1.2
        stream._mark(True)
        return stream

    ib.subscribe_tick_paced = ticking_subscribe
    return ib


@pytest.mark.asyncio
async def test_stream_loop_broadcasts_only_chain_tick_with_changed_fields(app_state, monkeypatch):
    st = _stream_state(app_state)
    st.contracts = _FakeRegistry(st)
    sent = []

    def on_pass(n):
        if n == 2:
            st.chain_stream_tickers[(5200.0, "C")].ask = 1.3         # the only quote that moves
        if n == 3:
            st.last_chain_update = "sentinel"                         # the quiet pass must leave it alone

    await _run_stream_loop(monkeypatch, _ticking_ib(), st, passes=4, sent=sent, on_pass=on_pass)

    assert {m["type"] for m in sent} == {"chain_tick"}               # no stream-scope chain_quotes
    first, second = sent[0]["data"]["ticks"], sent[1]["data"]["ticks"]
    assert len(first) == 10 and all(TICK_FIELDS <= set(t) for t in first)          # first pass: every field
    assert second == [{"strike": 5200.0, "right": "C", "ask": 1.3}]                 # then only what changed
    assert len(sent) == 2                                             # pass 3 changed nothing: no message
    assert st.last_chain_update == "sentinel"


@pytest.mark.asyncio
async def test_the_status_line_counts_every_collected_tick_even_when_none_changed(app_state, monkeypatch):
    st = _stream_state(app_state)
    st.contracts = _FakeRegistry(st)
    sent, status_args = [], []
    real_line = chain_manager.chain_stream_status_line

    def spy_line(*args):
        status_args.append(args)
        return real_line(*args)

    clock = itertools.count(0, 100)         # every read is 100 s later, so the 10 s throttle passes each pass
    monkeypatch.setattr(chain_manager, "time", types.SimpleNamespace(
        monotonic=lambda: float(next(clock)), perf_counter=time.perf_counter))
    monkeypatch.setattr(chain_manager, "chain_stream_status_line", spy_line)
    await _run_stream_loop(monkeypatch, _ticking_ib(), st, passes=4, sent=sent)

    assert len(sent) == 1                                             # only pass 1 had anything to send
    assert len(status_args) >= 3 and status_args[2] == (10, 10, 10)   # pass 3 is silent but still counted


@pytest.mark.asyncio
async def test_a_dropped_subscription_loses_its_last_sent_values(app_state, monkeypatch):
    st = _stream_state(app_state)
    st.contracts = _FakeRegistry(st)
    sent = []

    def on_pass(n):
        if n == 2:
            st.strikes = [s for s in st.strikes if s != 5210.0]      # 5210 leaves the focus: unsubscribed
        if n == 3:
            st.strikes = [5190.0, 5195.0, 5200.0, 5205.0, 5210.0]    # and comes back: a new subscription

    await _run_stream_loop(monkeypatch, _ticking_ib(), st, passes=5, sent=sent, on_pass=on_pass)

    assert len(sent) == 2 and len(sent[0]["data"]["ticks"]) == 10
    resent = sent[1]["data"]["ticks"]
    assert sorted((t["strike"], t["right"]) for t in resent) == [(5210.0, "C"), (5210.0, "P")]
    assert all(TICK_FIELDS <= set(t) for t in resent)               # same values as before, still sent whole


@pytest.mark.asyncio
async def test_an_expiry_switch_resends_every_field(app_state, monkeypatch):
    st = _stream_state(app_state)
    st.contracts = _FakeRegistry(st)
    sent = []

    def on_pass(n):
        if n == 2:
            st.expiration = NEW_EXP

    await _run_stream_loop(monkeypatch, _ticking_ib(), st, passes=4, sent=sent, on_pass=on_pass)

    assert len(sent) == 2
    resent = sent[1]["data"]["ticks"]
    assert len(resent) == 10 and all(TICK_FIELDS <= set(t) for t in resent)       # same values, new expiry
