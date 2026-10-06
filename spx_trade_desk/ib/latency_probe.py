"""Acceptance probe for the IB-layer latency work. Paper account only.

    python -m spx_trade_desk.ib.latency_probe [--port 7497] [--client-id 150] [--skip-bulk] [--out run.json]

Stop the dashboard first (it owns client id 1 and the market-data lines). The probe boots a
session through ``boot_session``, then places and cancels only non-fillable orders (limit <= $0.10
on an at-the-money SPXW put, or a put-spread debit worth several dollars) through the real
``handle_place_order`` / ``handle_cancel_order``, and prints every target with PASS / NEAR / MISS
(NEAR = within 10% of the target). It refuses to run unless the account code starts with "DU"
(paper), never prints account data, and sweeps any order still live when it finishes.
"""
import argparse
import asyncio
import json
import logging
import math
import sys
import time
from typing import Dict, List, Optional

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from spx_trade_desk.core import config
from spx_trade_desk.core.app_state import AppState
from spx_trade_desk.core.perf import perf
from spx_trade_desk.ib import orders
from spx_trade_desk.ib.client import IBClient
from spx_trade_desk.ib.connection import index_price
from spx_trade_desk.ib.contracts import ContractRegistry, norm_key
from spx_trade_desk.ib.session import boot_session

MAX_PROBE_LMT = 0.10
NEAR_FACTOR = 1.10
ERRS: List[tuple] = []          # (monotonic, reqId, code, message): codes and text from IB, never account data

# key -> (limit in ms, description). Values are the spec's targets (sections 1 and 8).
TARGETS = {
    "single_ack_p50": (100.0, "single-leg order, place to ack, p50"),
    "combo_ack_p50": (100.0, "put-spread combo, place to ack, p50"),
    "single_bracket_ack_p50": (150.0, "single leg + stop, place to ack, p50"),
    "combo_bracket_ack_p50": (150.0, "combo + stop, place to ack, p50"),
    "cancel_p50": (105.0, "cancel to confirmed terminal, p50"),
    "ack_during_fill_p95": (150.0, "order ack during a paced fill, p95"),
    "ack_during_bulk_max": (300.0, "order ack during the bulk request, max"),
    "bulk_first_call": (1000.0, "bulk qualification of the 0DTE chain, first call"),
    "bulk_failed_lookups": (0.0, "failed lookups (IB error 200) during the bulk call"),
    "boot": (1500.0, "boot_session, first boot"),
}


def verdict(value: Optional[float], limit: float) -> str:
    if value is None:
        return "N/A"
    if value <= limit:
        return "PASS"
    return "NEAR" if value <= limit * NEAR_FACTOR else "MISS"


def evaluate(measured: Dict[str, Optional[float]]) -> List[dict]:
    rows = []
    for key, (limit, description) in TARGETS.items():
        value = measured.get(key)
        rows.append({"key": key, "description": description, "value": value, "limit": limit,
                     "verdict": verdict(value, limit)})
    return rows


def format_table(rows: List[dict]) -> str:
    out = [f"{'check':56} {'measured':>10} {'target':>10}  verdict"]
    for r in rows:
        value = "-" if r["value"] is None else f"{r['value']:.0f}"
        out.append(f"{r['description']:56} {value:>10} {'<= ' + format(r['limit'], '.0f'):>10}  {r['verdict']}")
    return "\n".join(out)


def _pct(values: List[float], p: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    return s[max(0, min(len(s) - 1, math.ceil(p / 100.0 * len(s)) - 1))]


def _ms(seconds: float) -> float:
    return round(seconds * 1000.0, 1)


async def _timed(coro):
    t0 = time.perf_counter()
    result = await coro
    return result, _ms(time.perf_counter() - t0)


def _collect_error(req_id, code, message, contract=None, *args):
    ERRS.append((round(time.monotonic(), 2), req_id, code, message))


# -- safe payloads -----------------------------------------------------------------------------

def single_payload(expiry: str, strike: float, stop: bool = False) -> dict:
    p = {"legs": [{"secType": "OPT", "symbol": "SPX", "expiry": expiry, "strike": strike,
                   "right": "P", "action": "BUY", "qty": 1, "lmtPrice": 0.05}],
         "orderType": "LMT", "tif": "DAY"}
    if stop:
        p["stopLoss"] = {"stopPrice": 0.10, "limitPrice": 0.05}
    return p


def spread_payload(expiry: str, strike: float, stop: bool = False) -> dict:
    p = {"legs": [{"secType": "OPT", "symbol": "SPX", "expiry": expiry, "strike": strike,
                   "right": "P", "action": "BUY", "qty": 1, "lmtPrice": 0.05},
                  {"secType": "OPT", "symbol": "SPX", "expiry": expiry, "strike": strike - 5,
                   "right": "P", "action": "SELL", "qty": 1, "lmtPrice": 0.05}],
         "orderType": "LMT", "tif": "DAY", "comboAction": "BUY", "comboQuantity": 1,
         "comboLmtPrice": 0.05}
    if stop:
        p["stopLoss"] = {"stopPrice": 0.10, "limitPrice": 0.05}
    return p


def _assert_safe(payload: dict) -> None:
    """Refuse any payload that could fill or grow: every price <= $0.10, 1 lot, plain LMT."""
    if payload.get("orderType") != "LMT" or payload.get("dynamicFill"):
        raise ValueError("probe orders must be plain limit orders")
    if any(leg.get("qty") != 1 for leg in payload["legs"]):
        raise ValueError("probe orders are one lot")
    stop = payload.get("stopLoss") or {}
    prices = [leg.get("lmtPrice", 0.0) for leg in payload["legs"]]
    prices += [payload.get("comboLmtPrice", 0.0), stop.get("stopPrice", 0.0), stop.get("limitPrice", 0.0)]
    if max(prices) > MAX_PROBE_LMT:
        raise ValueError(f"probe refuses a price above ${MAX_PROBE_LMT:.2f}")


async def _assert_paper(ib, wait_s: float = 2.0) -> None:
    """Exit unless the connected account is a paper account. The code itself is never printed."""
    waited = 0.0
    while not getattr(ib, "_account_code", None) and waited < wait_s:
        await asyncio.sleep(0.1)
        waited += 0.1
    code = getattr(ib, "_account_code", None) or ""
    if not code.startswith("DU"):
        raise SystemExit("refusing to run: the connected account is not a paper account")


class _NoBulk:
    """The real client, except the bulk request fails: the registry takes its paced single fallback."""

    def __init__(self, ib):
        self._ib = ib

    async def req_chain_contract_details(self, *args, **kwargs):
        raise RuntimeError("bulk request disabled for the probe")

    def __getattr__(self, name):
        return getattr(self._ib, name)


# -- measurements --------------------------------------------------------------------------------

async def _place_cancel(ib, state, payload):
    """One order through the real handlers: (ack_ms, cancel_ms or None, status or error text)."""
    _assert_safe(payload)
    resp, ack_ms = await _timed(orders.handle_place_order(ib, state, payload, ws=None, refresh_fn=None))
    data = resp["data"]
    if data.get("status") == "Error":
        return None, None, data.get("message", "error")
    cancel, cancel_ms = await _timed(orders.handle_cancel_order(ib, state, data["orderId"]))
    status = cancel["data"]["status"]
    if data.get("stopOrderId"):
        ib.cancel_order(data["stopOrderId"])              # IB cancels children with the parent; be sure
    return ack_ms, (cancel_ms if status == "Cancelled" else None), status


async def _orders_section(ib, state, expiry, atm) -> dict:
    plan = (("single", single_payload, False, 5), ("combo", spread_payload, False, 5),
            ("single_bracket", single_payload, True, 3), ("combo_bracket", spread_payload, True, 3))
    out, cancels = {}, []
    for name, build, stop, reps in plan:
        acks, problems = [], []
        for _ in range(reps):
            ack, cancel, status = await _place_cancel(ib, state, build(expiry, atm, stop))
            if ack is None:
                problems.append(status)
            else:
                acks.append(ack)
            if cancel is not None:
                cancels.append(cancel)
            await asyncio.sleep(0.3)
        out[name] = {"acks_ms": acks, "problems": problems[:3]}
    out["cancels_ms"] = cancels
    return out


async def _during_fill(ib, state, expiry, atm, n_orders: int = 10) -> dict:
    """Orders must stay fast while 300 contracts qualify one by one through the pacer."""
    keys = [norm_key(atm + 5 * i, r) for i in range(-75, 75) for r in ("C", "P")]
    load = asyncio.create_task(
        ContractRegistry().qualify_keys(_NoBulk(ib), expiry, "SPXW", keys, time.monotonic()))
    acks = []
    try:
        await asyncio.sleep(0.5)                           # let the load reach the connection
        for _ in range(n_orders):
            ack, _cancel, _status = await _place_cancel(ib, state, single_payload(expiry, atm))
            if ack is not None:
                acks.append(ack)
            await asyncio.sleep(0.4)
        still_running = not load.done()
    finally:
        load.cancel()
        await asyncio.gather(load, return_exceptions=True)
    return {"acks_ms": acks, "load_still_running_at_the_end": still_running}


async def _bulk_section(args, expiry, atm) -> dict:
    """First bulk call on a fresh connection, with orders placed while its details stream in."""
    ib2 = IBClient()
    ib2.error_handler = _collect_error
    await ib2.connect(config.IB_HOST, args.port, args.client_id + 1, timeout=15)
    try:
        await _assert_paper(ib2)
        state2 = AppState()
        state2.connected = True
        await _place_cancel(ib2, state2, single_payload(expiry, atm))     # warm the order registry
        registry, errs_before = ContractRegistry(), len(ERRS)
        t0 = time.perf_counter()
        bulk = asyncio.create_task(registry.ensure_chain(ib2, "SPX", expiry, "SPXW", time.monotonic()))
        await asyncio.sleep(0.05)
        acks = []
        for _ in range(3):
            ack, _cancel, _status = await _place_cancel(ib2, state2, single_payload(expiry, atm))
            if ack is not None:
                acks.append(ack)
        listed = await bulk
        first_ms = _ms(time.perf_counter() - t0)
        failed = sum(1 for e in ERRS[errs_before:] if e[2] == 200) + (0 if listed >= 40 else 1)
        return {"first_call_ms": first_ms, "listed": listed, "failed_lookups": failed, "acks_ms": acks}
    finally:
        await _sweep(ib2)
        ib2.disconnect()


async def _sweep(ib) -> int:
    live = [oid for oid, h in list(ib.orders.items()) if not h.is_terminal()]
    for oid in live:
        ib.cancel_order(oid)
    if live:
        await asyncio.sleep(1.0)
    return sum(1 for h in ib.orders.values() if not h.is_terminal())


async def run(args) -> int:
    perf.reset()
    ib, state = IBClient(), AppState()
    result = {"args": {"port": args.port, "client_id": args.client_id}}
    try:
        await boot_session(ib, state, first_boot=True, port=args.port, client_id=args.client_id,
                           error_handler=_collect_error)
        await _assert_paper(ib)
        boot_ms = perf.snapshot()["metrics"]["startup.total"]["last"]
        spot = index_price(state.spx_stream) or state.spx_price
        if not state.expiration or spot <= 0:
            raise SystemExit("no expiration or spot price after boot: is the market data subscription live?")
        expiry, atm = state.expiration, float(round(spot / 5.0) * 5)
        result["market"] = {"expiry": expiry, "atm": atm}
        result["orders"] = await _orders_section(ib, state, expiry, atm)
        result["fill"] = await _during_fill(ib, state, expiry, atm)
        result["bulk"] = None if args.skip_bulk else await _bulk_section(args, expiry, atm)
    finally:
        result["live_orders_left"] = await _sweep(ib) if ib.connected else None
        ib.disconnect()

    o, bulk = result["orders"], result["bulk"]
    measured = {
        "single_ack_p50": _pct(o["single"]["acks_ms"], 50),
        "combo_ack_p50": _pct(o["combo"]["acks_ms"], 50),
        "single_bracket_ack_p50": _pct(o["single_bracket"]["acks_ms"], 50),
        "combo_bracket_ack_p50": _pct(o["combo_bracket"]["acks_ms"], 50),
        "cancel_p50": _pct(o["cancels_ms"], 50),
        "ack_during_fill_p95": _pct(result["fill"]["acks_ms"], 95),
        "ack_during_bulk_max": max(bulk["acks_ms"]) if bulk and bulk["acks_ms"] else None,
        "bulk_first_call": bulk["first_call_ms"] if bulk else None,
        "bulk_failed_lookups": float(bulk["failed_lookups"]) if bulk else None,
        "boot": boot_ms,
    }
    rows = evaluate(measured)
    result["rows"] = rows
    result["perf"] = perf.snapshot()
    result["ib_error_codes"] = sorted({e[2] for e in ERRS if e[2] not in (2104, 2106, 2107, 2108, 2158, 2119)})
    print(format_table(rows))
    print(f"live orders left after the sweep: {result['live_orders_left']}")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=1, default=str)
    return 1 if any(r["verdict"] == "MISS" for r in rows) else 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=7497)
    ap.add_argument("--client-id", type=int, default=150)
    ap.add_argument("--skip-bulk", action="store_true")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    logging.basicConfig(level=logging.ERROR)
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
