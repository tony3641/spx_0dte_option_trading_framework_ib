"""server.run_server: the entry point's event loop and shutdown behavior (no sockets, no IB)."""
import asyncio
import sys

from spx_trade_desk import server


class _FakeServer:
    def __init__(self, serve):
        self._serve = serve
        self.should_exit = False
        self.force_exit = False

    async def serve(self):
        return await self._serve(self)


def test_run_server_serves_on_the_selector_loop_on_windows():
    seen = {}

    async def serve(_srv):
        seen["selector"] = isinstance(asyncio.get_running_loop(), asyncio.SelectorEventLoop)

    server.run_server(_FakeServer(serve))
    if sys.platform == "win32":
        assert seen["selector"] is True
    else:
        assert "selector" in seen


def test_run_server_leaves_no_task_or_loop_running_after_it_returns():
    leftovers = []

    async def serve(_srv):
        async def forever():
            await asyncio.sleep(3600)

        leftovers.append(asyncio.ensure_future(forever()))     # a task the server forgot to cancel

    server.run_server(_FakeServer(serve))
    assert leftovers[0].cancelled()                             # Runner's exit cancelled it
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise AssertionError("a loop is still running after run_server returned")


def test_a_keyboard_interrupt_asks_the_server_to_exit_instead_of_propagating():
    async def serve(_srv):
        raise KeyboardInterrupt

    fake = _FakeServer(serve)
    server.run_server(fake)
    assert fake.should_exit is True
