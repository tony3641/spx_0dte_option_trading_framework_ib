"""Interactive Brokers adapter boundary.

Everything that speaks the ``ibapi`` wire protocol lives under this package.
``Contract`` is re-exported below because it is the only ``ibapi`` type that
non-ib code needs; import it from here rather than from ``ibapi`` directly.
"""

from ibapi.contract import Contract  # noqa: F401  (re-exported)

__all__ = ["Contract"]
