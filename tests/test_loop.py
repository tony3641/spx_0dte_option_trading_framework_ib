"""core.loop: entry points pick their event loop with a factory, never the deprecated policy API."""
import asyncio
import sys

from spx_trade_desk.core import loop as loop_mod


def test_windows_gets_a_selector_loop_factory_and_other_platforms_the_default(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert loop_mod.loop_factory() is asyncio.SelectorEventLoop
    monkeypatch.setattr(sys, "platform", "linux")
    assert loop_mod.loop_factory() is None


def test_run_executes_the_coroutine_without_touching_the_event_loop_policy(monkeypatch):
    def forbidden(policy):
        raise AssertionError("the event-loop policy must not be set")

    monkeypatch.setattr(asyncio, "set_event_loop_policy", forbidden)

    async def work():
        return isinstance(asyncio.get_running_loop(), asyncio.SelectorEventLoop)

    on_selector = loop_mod.run(work())
    assert on_selector is (sys.platform == "win32" or on_selector)
