"""core.loop: entry points pick their event loop with a factory, never the deprecated policy API."""
import asyncio
import sys
import warnings

from spx_trade_desk.core import loop as loop_mod


def test_windows_gets_a_selector_loop_factory_and_other_platforms_the_default(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert loop_mod.loop_factory() is asyncio.SelectorEventLoop
    monkeypatch.setattr(sys, "platform", "linux")
    assert loop_mod.loop_factory() is None


def _current_policy():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)        # the policy API is deprecated in 3.14
        return asyncio.get_event_loop_policy()


def test_run_executes_the_coroutine_without_touching_the_event_loop_policy(monkeypatch):
    def forbidden(policy):
        raise AssertionError("the event-loop policy must not be set")

    monkeypatch.setattr(asyncio, "set_event_loop_policy", forbidden)
    policy_before = _current_policy()

    async def work():
        return type(asyncio.get_running_loop())

    loop_type = loop_mod.run(work())
    if sys.platform == "win32":
        assert loop_type is asyncio.SelectorEventLoop              # not the Proactor default
    else:
        probe = asyncio.new_event_loop()                           # the platform default
        try:
            assert loop_type is type(probe)
        finally:
            probe.close()
    assert _current_policy() is policy_before                      # and the global policy is untouched
