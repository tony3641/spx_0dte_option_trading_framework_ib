"""boot_session / start_background_loops: step sets, ordering, fail-fast, threading, prefetch,
and the server wiring that uses them."""
import asyncio
import logging
import threading

import pytest

from spx_trade_desk.core.perf import perf
from spx_trade_desk.ib import session
from tests.conftest import MockIBClient

FIRST_BOOT_STEPS = {"connect", "spx", "chain_info", "monthly_info", "hist_bars", "es", "es_baseline",
                    "account", "vix", "vix1d"}
STEP_ATTRS = {"connect": "connect_ib", "spx": "setup_spx_subscription",
              "chain_info": "setup_chain_info", "monthly_info": "setup_monthly_chain_info",
              "hist_bars": "fetch_historical_bars", "es": "setup_es_subscription",
              "es_baseline": "fetch_es_baseline", "account": "setup_account_subscription",
              "vix": "setup_vix_subscription", "vix1d": "setup_vix1d_subscription"}


class Rec:
    """Replaces every boot step in the session namespace with a recorder."""

    def __init__(self, monkeypatch, rth=True):
        self.events = []
        self.calls = {}                    # step -> (args, kwargs)
        self.hooks = {}                    # step -> async callable awaited inside the step
        self.rth = rth
        self.rate_thread = None
        for name, attr in STEP_ATTRS.items():
            monkeypatch.setattr(session, attr, self._step(name))
        monkeypatch.setattr(session, "get_risk_free_rate", self._rate)
        monkeypatch.setattr(session, "load_strategies", self._strategies)
        monkeypatch.setattr(session, "is_within_rth", lambda: self.rth)

    def _step(self, name):
        async def fake(*args, **kwargs):
            self.events.append(f"start:{name}")
            self.calls[name] = (args, kwargs)
            hook = self.hooks.get(name)
            if hook is not None:
                await hook()
            self.events.append(f"end:{name}")
        return fake

    def _rate(self):
        self.rate_thread = threading.get_ident()
        self.events.append("sgov")
        return 0.0425

    def _strategies(self):
        self.events.append("strategies")
        return {"MyStrategy": object()}

    def started(self):
        return {e[6:] for e in self.events if e.startswith("start:")}


@pytest.fixture
def rec(monkeypatch):
    return Rec(monkeypatch)


@pytest.mark.asyncio
async def test_first_boot_outside_rth_runs_every_step(monkeypatch, app_state):
    r = Rec(monkeypatch, rth=False)
    await session.boot_session(MockIBClient(), app_state, first_boot=True)
    assert r.started() == FIRST_BOOT_STEPS
    assert "sgov" in r.events and "strategies" in r.events
    assert app_state.risk_free_rate == 0.0425
    assert "MyStrategy" in app_state.strategies


@pytest.mark.asyncio
async def test_first_boot_inside_rth_skips_only_the_es_baseline(rec, app_state):
    await session.boot_session(MockIBClient(), app_state, first_boot=True)
    assert rec.started() == FIRST_BOOT_STEPS - {"es_baseline"}
    assert "sgov" in rec.events


@pytest.mark.asyncio
async def test_reconnect_runs_exactly_todays_reconnect_step_set(monkeypatch, app_state):
    r = Rec(monkeypatch, rth=False)             # outside RTH the baseline would run on a first boot
    await session.boot_session(MockIBClient(), app_state, first_boot=False)
    assert r.started() == {"connect", "spx", "chain_info", "monthly_info", "es", "account", "vix", "vix1d"}
    assert "sgov" not in r.events
    assert "strategies" in r.events


@pytest.mark.asyncio
async def test_connect_gets_the_port_and_client_id_and_the_error_handler_is_installed(rec, app_state):
    ib = MockIBClient()
    handler = lambda *a: None
    await session.boot_session(ib, app_state, first_boot=False, port=4002, client_id=77, error_handler=handler)
    assert rec.calls["connect"][1] == {"port": 4002, "client_id": 77}
    assert ib.error_handler is handler


@pytest.mark.asyncio
async def test_chain_info_and_history_start_only_after_spx_is_subscribed(rec, app_state):
    await session.boot_session(MockIBClient(), app_state, first_boot=True)
    end_spx = rec.events.index("end:spx")
    for step in ("chain_info", "monthly_info", "hist_bars"):
        assert rec.events.index(f"start:{step}") > end_spx


@pytest.mark.asyncio
async def test_independent_branches_overlap(rec, app_state):
    gate = asyncio.Event()

    async def spx_waits_for_es():
        await gate.wait()

    async def es_opens_the_gate():
        gate.set()

    rec.hooks["spx"] = spx_waits_for_es
    rec.hooks["es"] = es_opens_the_gate
    # run one after the other this would wait for the gate forever
    await asyncio.wait_for(session.boot_session(MockIBClient(), app_state, first_boot=True), timeout=2)


@pytest.mark.asyncio
async def test_the_first_failure_cancels_the_sibling_steps_and_aborts_the_boot(rec, app_state):
    vix_started = asyncio.Event()

    async def vix_stalls():
        vix_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            rec.events.append("cancelled:vix")
            raise

    async def account_fails_once_vix_runs():
        await vix_started.wait()
        raise RuntimeError("account refused")

    rec.hooks["vix"] = vix_stalls
    rec.hooks["account"] = account_fails_once_vix_runs
    with pytest.raises(RuntimeError, match="account refused"):
        await asyncio.wait_for(session.boot_session(MockIBClient(), app_state, first_boot=True), timeout=2)
    assert "cancelled:vix" in rec.events
    assert "strategies" not in rec.events                 # no half-booted desk
    assert app_state.background_tasks == []


@pytest.mark.asyncio
async def test_a_failed_connect_stops_the_boot_before_any_other_step(rec, app_state):
    async def refused():
        raise ConnectionError("refused")

    rec.hooks["connect"] = refused
    with pytest.raises(ConnectionError):
        await session.boot_session(MockIBClient(), app_state, first_boot=True)
    assert rec.started() == {"connect"}


@pytest.mark.asyncio
async def test_the_risk_free_rate_is_fetched_in_a_worker_thread(rec, app_state):
    await session.boot_session(MockIBClient(), app_state, first_boot=True)
    assert rec.rate_thread is not None and rec.rate_thread != threading.get_ident()


@pytest.mark.asyncio
async def test_boot_records_the_startup_span(rec, app_state):
    perf.reset()
    await session.boot_session(MockIBClient(), app_state, first_boot=False)
    assert perf.snapshot()["metrics"]["startup.total"]["n"] == 1


@pytest.mark.asyncio
async def test_boot_starts_a_chain_prefetch_without_waiting_for_it(rec, app_state, monkeypatch):
    app_state.expiration = "20261006"
    gate = asyncio.Event()
    seen = []

    async def slow_listing(ib, symbol, expiry, trading_class, now, force=False):
        seen.append((symbol, expiry, trading_class))
        await gate.wait()
        return 3

    monkeypatch.setattr(app_state.contracts, "ensure_chain", slow_listing)
    await asyncio.wait_for(session.boot_session(MockIBClient(), app_state, first_boot=False), timeout=2)
    await asyncio.sleep(0)
    assert seen == [("SPX", "20261006", "SPXW")]
    (task,) = app_state.background_tasks
    assert not task.done()                                # the boot returned while the listing is pending
    gate.set()
    await task


@pytest.mark.asyncio
async def test_a_failed_prefetch_is_logged_not_raised(rec, app_state, monkeypatch, caplog):
    app_state.expiration = "20261006"

    async def broken(*a, **k):
        raise RuntimeError("secdef farm down")

    monkeypatch.setattr(app_state.contracts, "ensure_chain", broken)
    with caplog.at_level(logging.WARNING):
        await session.boot_session(MockIBClient(), app_state, first_boot=False)
        await asyncio.gather(*app_state.background_tasks)      # must not raise
    assert "secdef farm down" in caplog.text


@pytest.mark.asyncio
async def test_no_prefetch_without_an_expiration(rec, app_state):
    app_state.expiration = None
    await session.boot_session(MockIBClient(), app_state, first_boot=False)
    assert app_state.background_tasks == []


LOOPS = ("price_push_loop", "status_push_loop", "account_push_loop", "log_push_loop",
         "strategy_evaluation_loop", "take_profit_loop", "chain_poll_loop", "chain_stream_loop",
         "chain_publish_loop")


@pytest.mark.asyncio
async def test_start_background_loops_starts_the_nine_session_loops(monkeypatch, app_state):
    started = {}

    def stub(name):
        async def run(*args, **kwargs):
            started[name] = (args, kwargs)
            await asyncio.Event().wait()
        return run

    for attr in LOOPS:
        monkeypatch.setattr(session, attr, stub(attr))
    recorder, bcast, ib = object(), object(), object()
    session.start_background_loops(ib, app_state, bcast, recorder=recorder)
    await asyncio.sleep(0)
    assert set(started) == set(LOOPS) and len(app_state.background_tasks) == 9
    assert started["chain_publish_loop"][1] == {"recorder": recorder}
    assert started["status_push_loop"][0] == (app_state, bcast)
    assert app_state.force_chain_fetch_event is not None
    for t in app_state.background_tasks:
        t.cancel()
    await asyncio.gather(*app_state.background_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_start_background_loops_keeps_an_existing_force_event(monkeypatch, app_state):
    for attr in LOOPS:
        async def idle(*a, **k):
            await asyncio.Event().wait()
        monkeypatch.setattr(session, attr, idle)
    existing = asyncio.Event()
    app_state.force_chain_fetch_event = existing
    session.start_background_loops(object(), app_state, object(), recorder=None)
    assert app_state.force_chain_fetch_event is existing
    for t in app_state.background_tasks:
        t.cancel()
    await asyncio.gather(*app_state.background_tasks, return_exceptions=True)


# -- server wiring: the manual reconnect goes through boot_session -----------------------------

@pytest.fixture
def server_stub(monkeypatch, app_state):
    from spx_trade_desk import server
    calls = []

    class FakeIB:
        def unsubscribe_all(self):
            calls.append("unsubscribe_all")

        def disconnect(self):
            calls.append("disconnect")

    async def fake_boot(ib, state, **kw):
        calls.append(("boot", kw))

    def fake_loops(ib, state, bcast, *, recorder):
        calls.append(("loops", recorder))

    async def fake_broadcast(state, payload):
        calls.append("broadcast")

    monkeypatch.setattr(server, "IBClient", FakeIB)
    monkeypatch.setattr(server, "ib", FakeIB())
    monkeypatch.setattr(server, "state", app_state)
    monkeypatch.setattr(server, "discord_manager", None)
    monkeypatch.setattr(server, "broadcast_fn", None)
    monkeypatch.setattr(server, "boot_session", fake_boot)
    monkeypatch.setattr(server, "start_background_loops", fake_loops)
    monkeypatch.setattr(server, "broadcast", fake_broadcast)
    return server, calls


@pytest.mark.asyncio
async def test_reconnect_boots_in_reconnect_mode_then_starts_the_loops(server_stub, app_state):
    server, calls = server_stub
    out = await server.reconnect_ib_on(7497)
    assert out == {"status": "ok", "port": 7497}
    boot = next(c for c in calls if isinstance(c, tuple) and c[0] == "boot")
    loops = next(c for c in calls if isinstance(c, tuple) and c[0] == "loops")
    assert boot[1]["first_boot"] is False and boot[1]["port"] == 7497 and callable(boot[1]["error_handler"])
    assert loops[1] is server.chain_recorder
    assert calls.index(boot) < calls.index(loops)
    assert "broadcast" in calls


@pytest.mark.asyncio
async def test_a_failed_reconnect_boot_starts_no_loops_and_answers_500(server_stub, monkeypatch):
    server, calls = server_stub

    async def refused(ib, state, **kw):
        raise ConnectionError("refused")

    monkeypatch.setattr(server, "boot_session", refused)
    with pytest.raises(server.HTTPException) as ei:
        await server.reconnect_ib_on(7497)
    assert ei.value.status_code == 500
    assert not any(isinstance(c, tuple) and c[0] == "loops" for c in calls)
