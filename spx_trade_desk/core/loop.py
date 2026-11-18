"""Event-loop selection for entry points (server, IB probes).

The global event-loop policy API (``asyncio.set_event_loop_policy`` and the ``Windows*EventLoopPolicy``
classes) is deprecated in Python 3.14 and removed in 3.16. Entry points pass a loop factory to
``asyncio.run`` / ``asyncio.Runner`` instead, so nothing here changes global state and importing a module
never has an event-loop side effect.
"""

import asyncio
import sys
from typing import Any, Callable, Coroutine, Optional


def loop_factory() -> Optional[Callable[[], asyncio.AbstractEventLoop]]:
    """Selector loop on Windows (the Proactor default is avoided); the platform default elsewhere."""
    return asyncio.SelectorEventLoop if sys.platform == "win32" else None


def run(coro: Coroutine[Any, Any, Any]) -> Any:
    """``asyncio.run`` on the loop this project wants."""
    return asyncio.run(coro, loop_factory=loop_factory())
