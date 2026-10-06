"""Acceptance probe for the IB-layer latency work. Paper account only.

    python -m spx_trade_desk.ib.latency_probe [--port 7497] [--client-id 150] [--skip-bulk] [--out run.json]

Stop the dashboard first (it owns client id 1 and the market-data lines). The probe boots a
session through ``boot_session``, then places and cancels only non-fillable orders (limit <= $0.10
on an at-the-money SPXW put, or a put-spread debit worth several dollars) through the real
``handle_place_order`` / ``handle_cancel_order``, and prints every target with PASS / NEAR / MISS
(NEAR = within 10% of the target). It refuses to run unless the account code starts with "DU"
(paper) and refuses the standard live ports, never prints account data, and sweeps any order still
live when it finishes.

A placement error or a cancel that IB did not confirm counts as a failure and makes the rows of
its section MISS; a slow cancel is still timed. The exit code is 1 on any MISS, on any row that
could not be measured (unless --skip-bulk skipped it) and on any order left live.
"""
import argparse
import asyncio
import json
import logging
import math
import sys
import time
from typing import Dict, List, NamedTuple, Optional

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from spx_trade_desk.core import config
from spx_trade_desk.core.app_state import AppState
from spx_trade_desk.core.perf import perf
from spx_trade_desk.ib import orders
from spx_trade_desk.ib.client import IBClient
from spx_trade_desk.ib.connection import index_price
from spx_trade_desk.ib.contracts import MIN_BULK_CONTRACTS, ContractRegistry, norm_key
from spx_trade_desk.ib.session import boot_session

MAX_PROBE_LMT = 0.10
NEAR_FACTOR = 1.10
LIVE_PORTS = (7496, 4001)               # standard TWS and Gateway live-account ports
# Client ids the probe must never take: 0 falls back to the dashboard id inside connect_ib, 1 is the
# dashboard, 97 the standalone chain capture, 140 line_probe. The probe also connects as N + 1.
RESERVED_CLIENT_IDS = (0, 1, 97, 140)
# Pauses of the live run, module constants so the tests can zero them.
ORDER_GAP_S = 0.3                       # between the orders of the orders section
FILL_LEAD_S = 0.5                       # let the qualification load reach the connection
FILL_GAP_S = 0.4                        # between the orders placed during the fill
BULK_LEAD_S = 0.05                      # let the bulk request go out before the first order
SWEEP_WAIT_S = 1.0                      # after each round of cancels in the final sweep

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


def evaluate(measured: Dict[str, Optional[float]], failed: Optional[Dict[str, int]] = None,
             samples: Optional[Dict[str, int]] = None, notes: Optional[Dict[str, str]] = None,
             skipped=()) -> List[dict]:
    """One row per target. ``failed`` counts placement errors and unconfirmed cancels per check: any
    failure makes the row MISS whatever its measured value. A key in ``skipped`` was legitimately not
    run: it stays N/A and is flagged so the exit code ignores it."""
    failed, samples, notes = failed or {}, samples or {}, notes or {}
    rows = []
    for key, (limit, description) in TARGETS.items():
        value = measured.get(key)
        bad = int(failed.get(key, 0) or 0)
        is_skipped = key in skipped
        rows.append({"key": key, "description": description, "value": value, "limit": limit,
                     "verdict": "MISS" if bad > 0 else verdict(value, limit),
                     "n": samples.get(key), "failed": bad, "skipped": is_skipped,
                     "note": notes.get(key) or ("skipped" if is_skipped else "")})
    return rows


def format_table(rows: List[dict]) -> str:
    out = [f"{'check':56} {'measured':>10} {'target':>10} {'n':>4} {'fail':>5}  verdict"]
    for r in rows:
        value = "-" if r["value"] is None else f"{r['value']:.1f}"
        n = "-" if r.get("n") is None else str(r["n"])
        note = f" ({r['note']})" if r.get("note") else ""
        out.append(f"{r['description']:56} {value:>10} {'<= ' + format(r['limit'], '.0f'):>10} "
                   f"{n:>4} {r.get('failed', 0):>5}  {r['verdict']}{note}")
    return "\n".join(out)


def exit_code(rows: List[dict], live_orders_left: Optional[int] = 0) -> int:
    """1 on any MISS, on a row that was not measured and not skipped, or on an order left live
    (or a leftover count that could not be taken); 0 otherwise."""
    for r in rows:
        if r["verdict"] == "MISS" or (r["verdict"] == "N/A" and not r.get("skipped")):
            return 1
    return 1 if live_orders_left is None or live_orders_left > 0 else 0


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


_TOP_KEYS = frozenset({"legs", "orderType", "tif", "comboAction", "comboQuantity", "comboLmtPrice", "stopLoss"})
_LEG_KEYS = frozenset({"secType", "symbol", "expiry", "strike", "right", "action", "qty", "lmtPrice"})
_COMBO_KEYS = frozenset({"comboAction", "comboQuantity", "comboLmtPrice"})


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _probe_price(value, what: str, signed: bool = False) -> None:
    """A real number in (0, MAX_PROBE_LMT] once abs() is applied, as the single-leg and stop handlers
    apply it. ``signed`` is for the combo limit, which the handler keeps signed (negative = credit):
    there the value itself must be positive."""
    if not _is_number(value):
        raise ValueError(f"probe {what} must be a number")
    if signed and value < 0:
        raise ValueError(f"probe {what} must not be negative")
    price = abs(float(value))
    if not math.isfinite(price) or not 0.0 < price <= MAX_PROBE_LMT:
        raise ValueError(f"probe {what} must be above 0 and at most ${MAX_PROBE_LMT:.2f}")


def _assert_safe(payload: dict) -> None:
    """Closed whitelist: accept exactly what ``single_payload`` / ``spread_payload`` emit, else raise.

    A single BUY of one SPX put, or a one-lot BUY put debit spread (long the higher strike, same
    expiry), as a plain DAY limit order. Every price present anywhere (leg limits, combo limit, stop
    trigger and limit) must be a real number in (0, $0.10] after abs(); unknown keys, a SELL single,
    a credit spread, three legs, GTC, outside-RTH and dynamic fill are all refused.
    """
    if not isinstance(payload, dict):
        raise ValueError("probe payload must be a dict")
    extra = set(payload) - _TOP_KEYS
    if extra:
        raise ValueError(f"probe payload has unexpected keys: {sorted(extra)}")
    if payload.get("orderType") != "LMT":
        raise ValueError("probe orders must be plain limit orders")
    if payload.get("tif") != "DAY":
        raise ValueError("probe orders must be DAY orders")
    legs = payload.get("legs")
    if not isinstance(legs, list) or len(legs) not in (1, 2):
        raise ValueError("probe orders have one or two legs")
    for leg in legs:
        if not isinstance(leg, dict) or set(leg) - _LEG_KEYS:
            raise ValueError("probe legs carry unexpected keys")
        if leg.get("secType") != "OPT" or leg.get("symbol") != "SPX" or leg.get("right") != "P":
            raise ValueError("probe legs are SPX puts")
        if type(leg.get("qty")) is not int or leg["qty"] != 1:
            raise ValueError("probe orders are one lot")
        if not _is_number(leg.get("strike")) or leg["strike"] <= 0 or not isinstance(leg.get("expiry"), str):
            raise ValueError("probe legs need an expiry and a positive strike")
        _probe_price(leg.get("lmtPrice"), "leg limit")
    if len(legs) == 1:
        if legs[0].get("action") != "BUY":
            raise ValueError("a single probe leg is a BUY")
        if _COMBO_KEYS & set(payload):
            raise ValueError("a single probe leg carries no combo fields")
    else:
        long_leg, short_leg = legs
        if (long_leg.get("action"), short_leg.get("action")) != ("BUY", "SELL"):
            raise ValueError("a probe spread buys its first leg and sells its second")
        if long_leg["expiry"] != short_leg["expiry"] or not long_leg["strike"] > short_leg["strike"]:
            raise ValueError("a probe spread is a debit put spread: long the higher strike, one expiry")
        if payload.get("comboAction") != "BUY":
            raise ValueError("a probe spread is a BUY combo")
        if type(payload.get("comboQuantity")) is not int or payload["comboQuantity"] != 1:
            raise ValueError("probe combos are one lot")
        _probe_price(payload.get("comboLmtPrice"), "combo limit", signed=True)
    if "stopLoss" in payload:
        stop = payload["stopLoss"]
        if not isinstance(stop, dict) or set(stop) != {"stopPrice", "limitPrice"}:
            raise ValueError("probe stop needs exactly a stopPrice and a limitPrice")
        _probe_price(stop["stopPrice"], "stop price")
        _probe_price(stop["limitPrice"], "stop limit")


def _refuse_live_port(port: int) -> None:
    """Exit before connecting when the port is a standard live-account port."""
    if port in LIVE_PORTS:
        raise SystemExit(f"refusing to run: port {port} is a live-account port "
                         f"(paper TWS is 7497, paper Gateway 4002)")


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

class Sample(NamedTuple):
    """One order through the real handlers."""
    ack_ms: Optional[float]          # place to ack; None when the placement itself errored
    cancel_ms: Optional[float]       # cancel to IB's answer; kept when slow, None when there was no cancel to time
    confirmed: bool                  # the cancel reached "Cancelled"
    status: str                      # the cancel status, or the placement error text
    during: Optional[bool] = None    # what the ``watch`` callable said when the ack arrived


def _new_tally() -> dict:
    return {"n": 0, "acks_ms": [], "cancels_ms": [], "placement_errors": 0, "unconfirmed_cancels": 0,
            "problems": []}


def _record(tally: dict, s: Sample) -> None:
    tally["n"] += 1
    if s.ack_ms is None:
        tally["placement_errors"] += 1
    else:
        tally["acks_ms"].append(s.ack_ms)
        if not s.confirmed:
            tally["unconfirmed_cancels"] += 1
    if s.cancel_ms is not None:
        tally["cancels_ms"].append(s.cancel_ms)
    if (s.ack_ms is None or not s.confirmed) and len(tally["problems"]) < 3:
        tally["problems"].append(s.status)


async def _place_cancel(ib, state, payload, watch=None) -> Sample:
    """One order through the real handlers. ``watch()`` is read the moment the ack arrives."""
    _assert_safe(payload)
    resp, ack_ms = await _timed(orders.handle_place_order(ib, state, payload, ws=None, refresh_fn=None))
    during = None if watch is None else bool(watch())
    data = resp["data"]
    if data.get("status") == "Error":
        return Sample(None, None, False, data.get("message", "error"), during)
    cancel, cancel_ms = await _timed(orders.handle_cancel_order(ib, state, data["orderId"]))
    status = cancel["data"]["status"]
    if data.get("stopOrderId"):
        ib.cancel_order(data["stopOrderId"])              # IB cancels children with the parent; be sure
    timed = status in ("Cancelled", "PendingCancel")      # a cancel that timed out is exactly the slow case
    return Sample(ack_ms, cancel_ms if timed else None, status == "Cancelled", status, during)


async def _orders_section(ib, state, expiry, atm, out: dict) -> dict:
    plan = (("single", single_payload, False, 5), ("combo", spread_payload, False, 5),
            ("single_bracket", single_payload, True, 3), ("combo_bracket", spread_payload, True, 3))
    for name, build, stop, reps in plan:
        tally = out[name] = _new_tally()
        for _ in range(reps):
            _record(tally, await _place_cancel(ib, state, build(expiry, atm, stop)))
            await asyncio.sleep(ORDER_GAP_S)
    return out


async def _during_fill(ib, state, expiry, atm, out: dict, n_orders: int = 10) -> dict:
    """Orders must stay fast while 300 contracts qualify one by one through the pacer."""
    out.update(_new_tally())
    out["load_still_running_at_the_end"] = None
    keys = [norm_key(atm + 5 * i, r) for i in range(-75, 75) for r in ("C", "P")]
    load = asyncio.create_task(
        ContractRegistry().qualify_keys(_NoBulk(ib), expiry, "SPXW", keys, time.monotonic()))
    try:
        await asyncio.sleep(FILL_LEAD_S)                   # let the load reach the connection
        for _ in range(n_orders):
            _record(out, await _place_cancel(ib, state, single_payload(expiry, atm)))
            await asyncio.sleep(FILL_GAP_S)
        out["load_still_running_at_the_end"] = not load.done()
    finally:
        load.cancel()
        await asyncio.gather(load, return_exceptions=True)
    return out


async def _bulk_section(args, expiry, atm, out: dict) -> dict:
    """First bulk call on a fresh connection, with orders placed while its details stream in.

    ``first_call_ms`` times the listing task alone. An ack counts for ``ack_during_bulk_max`` only if
    the listing was still running when the ack arrived; the others are kept in ``acks_ms`` for the record.
    """
    ib2 = IBClient()
    ib2.error_handler = _collect_error
    await ib2.connect(config.IB_HOST, args.port, args.client_id + 1, timeout=15)
    out.update(_new_tally())
    out.update(acks_during_bulk_ms=[], overlapped=0, warmup_failed=0, live_orders_left=None)
    bulk = None
    try:
        await _assert_paper(ib2)
        state2 = AppState()
        state2.connected = True
        warm = await _place_cancel(ib2, state2, single_payload(expiry, atm))     # warm the order registry
        out["warmup_failed"] = int(warm.ack_ms is None or not warm.confirmed)
        registry, errs_before = ContractRegistry(), len(ERRS)
        bulk = asyncio.create_task(
            _timed(registry.ensure_chain(ib2, "SPX", expiry, "SPXW", time.monotonic())))
        await asyncio.sleep(BULK_LEAD_S)
        for _ in range(3):
            s = await _place_cancel(ib2, state2, single_payload(expiry, atm), watch=lambda: not bulk.done())
            _record(out, s)
            if s.ack_ms is not None and s.during:
                out["acks_during_bulk_ms"].append(s.ack_ms)
        out["overlapped"] = len(out["acks_during_bulk_ms"])
        listed, first_ms = await bulk
        failed = sum(1 for e in ERRS[errs_before:] if e[2] == 200) + (0 if listed >= MIN_BULK_CONTRACTS else 1)
        out.update(first_call_ms=first_ms, listed=listed, failed_lookups=failed)
        return out
    finally:
        try:
            if bulk is not None and not bulk.done():
                bulk.cancel()
                await asyncio.gather(bulk, return_exceptions=True)
            out["live_orders_left"] = await _sweep(ib2)
        finally:
            ib2.disconnect()


async def _sweep(ib) -> int:
    """Cancel every order still live, wait, re-check, retry once; return how many are still live."""
    for _ in range(2):
        live = [oid for oid, h in list(ib.orders.items()) if not h.is_terminal()]
        if not live:
            return 0
        for oid in live:
            ib.cancel_order(oid)
        await asyncio.sleep(SWEEP_WAIT_S)
    return sum(1 for h in ib.orders.values() if not h.is_terminal())


# -- grading -------------------------------------------------------------------------------------

_ORDER_SECTIONS = ("single", "combo", "single_bracket", "combo_bracket")
_BULK_KEYS = ("ack_during_bulk_max", "bulk_first_call", "bulk_failed_lookups")


def grade(result: dict, skip_bulk: bool = False) -> List[dict]:
    """Rows for whatever was measured so far (a partial result grades without raising).

    Any placement error or unconfirmed cancel in a section fails that section's rows; a section that
    never ran leaves its rows N/A, which the exit code treats as a failure unless it was skipped.
    """
    measured, failed, samples, notes = {}, {}, {}, {}
    sections = result.get("orders") or {}
    cancels, unconfirmed, ran = [], 0, False
    for name in _ORDER_SECTIONS:
        sec = sections.get(name)
        if not sec:
            continue
        key = f"{name}_ack_p50"
        measured[key] = _pct(sec["acks_ms"], 50)
        samples[key] = len(sec["acks_ms"])
        failed[key] = sec["placement_errors"] + sec["unconfirmed_cancels"]
        cancels += sec["cancels_ms"]
        unconfirmed += sec["unconfirmed_cancels"]
        ran = True
    if ran:
        measured["cancel_p50"], samples["cancel_p50"], failed["cancel_p50"] = _pct(cancels, 50), len(cancels), unconfirmed

    fill = result.get("fill")
    if fill:
        measured["ack_during_fill_p95"] = _pct(fill["acks_ms"], 95)
        samples["ack_during_fill_p95"] = len(fill["acks_ms"])
        failed["ack_during_fill_p95"] = fill["placement_errors"] + fill["unconfirmed_cancels"]

    skipped = set(_BULK_KEYS) if skip_bulk else set()
    for key in skipped:
        notes[key] = "skipped (--skip-bulk)"
    bulk = None if skip_bulk else result.get("bulk")
    if bulk:
        if "first_call_ms" in bulk:
            measured["bulk_first_call"], samples["bulk_first_call"] = bulk["first_call_ms"], 1
            failed["bulk_first_call"] = int(bulk.get("listed", 0) < MIN_BULK_CONTRACTS)
            measured["bulk_failed_lookups"] = float(bulk["failed_lookups"])
        if "acks_during_bulk_ms" in bulk:
            acks = bulk["acks_during_bulk_ms"]
            samples["ack_during_bulk_max"] = len(acks)
            failed["ack_during_bulk_max"] = (bulk.get("placement_errors", 0) + bulk.get("unconfirmed_cancels", 0)
                                             + bulk.get("warmup_failed", 0))
            if acks:
                measured["ack_during_bulk_max"] = max(acks)
            else:
                notes["ack_during_bulk_max"] = "no order ack overlapped the bulk call"

    if result.get("boot_ms") is not None:
        measured["boot"], samples["boot"] = result["boot_ms"], 1
    return evaluate(measured, failed, samples, notes, skipped)


def _total_left(main_left: Optional[int], bulk: Optional[dict]) -> Optional[int]:
    bulk_left = (bulk or {}).get("live_orders_left")
    if main_left is None and bulk_left is None:
        return None
    return (main_left or 0) + (bulk_left or 0)


def _write_out(args, result: dict) -> None:
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=1, default=str)


async def run(args) -> int:
    _refuse_live_port(args.port)
    perf.reset()
    ib, state = IBClient(), AppState()
    result = {"args": {"port": args.port, "client_id": args.client_id}, "orders": {}, "fill": {},
              "bulk": None if args.skip_bulk else {}}
    try:
        await boot_session(ib, state, first_boot=True, port=args.port, client_id=args.client_id,
                           error_handler=_collect_error)
        await _assert_paper(ib)
        result["boot_ms"] = perf.snapshot()["metrics"].get("startup.total", {}).get("last")
        spot = index_price(state.spx_stream) or state.spx_price
        if not state.expiration or spot <= 0:
            raise SystemExit("no expiration or spot price after boot: is the market data subscription live?")
        expiry, atm = state.expiration, float(round(spot / 5.0) * 5)
        result["market"] = {"expiry": expiry, "atm": atm}
        await _orders_section(ib, state, expiry, atm, result["orders"])
        await _during_fill(ib, state, expiry, atm, result["fill"])
        if result["bulk"] is not None:
            await _bulk_section(args, expiry, atm, result["bulk"])
    finally:
        # Every exit path (including a section that raised or a refusal) sweeps, disconnects, reports the
        # leftover count and writes whatever was measured so far.
        main_left = None
        try:
            main_left = await _sweep(ib) if ib.connected else None
        finally:
            ib.disconnect()
            result["live_orders_left"] = _total_left(main_left, result["bulk"])
            result["rows"] = grade(result, args.skip_bulk)
            result["perf"] = perf.snapshot()
            result["ib_error_codes"] = sorted(
                {e[2] for e in ERRS if e[2] not in (2104, 2106, 2107, 2108, 2158, 2119)})
            print(f"live orders left after the sweep: {result['live_orders_left']}")
            _write_out(args, result)
    print(format_table(result["rows"]))
    return exit_code(result["rows"], result["live_orders_left"])


def _client_id(text: str) -> int:
    n = int(text)
    if n < 2 or any(c in RESERVED_CLIENT_IDS for c in (n, n + 1)):
        raise argparse.ArgumentTypeError(
            f"client id {n} (or {n} + 1, the second connection) collides with the dashboard (1), "
            f"the chain capture (97) or line_probe (140), or falls back to the dashboard id (0)")
    return n


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=7497, help="paper TWS 7497 (default) or paper Gateway 4002")
    ap.add_argument("--client-id", type=_client_id, default=150,
                    help="client id N; the bulk section also connects as N + 1")
    ap.add_argument("--skip-bulk", action="store_true", help="skip the second-connection bulk section")
    ap.add_argument("--out", default="", help="write the full result as JSON")
    return ap


def main() -> None:
    args = _parser().parse_args()
    logging.basicConfig(level=logging.ERROR)
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
