"""Calibrate the market-data line allowance of the account behind the API port.

    python -m spx_trade_desk.ib.line_probe [--cap 2000] [--client-id 140] [--port 7497]

IB's allowance is the greater of 100, monthly commissions / 8 and equity x 100 / 1,000,000, plus
100 per Quote Booster pack, shared by every API client and the TWS watchlists. No API call reports
it; the only API signal is error 101. This tool ramps ``reqMktData`` on listed SPXW contracts at
25 requests/s (bypassing the app's line budget) until IB refuses one, ``--cap`` is reached or the
listed contracts run out, disconnects (TWS drops the lines) and prints what it found. Stop the
dashboard and close TWS watchlists first, otherwise their lines reduce the result. It only reads
market data.
"""
import argparse
import asyncio
import math
import sys
from typing import List, Tuple

from spx_trade_desk.core import config
from spx_trade_desk.ib.client import IBClient
from spx_trade_desk.ib.connection import _index_contract
from spx_trade_desk.ib.pacing import RequestPacer
from spx_trade_desk.market.chain_fetcher import get_chain_params

SAFETY_FACTOR = 0.9
RAMP_RATE = 25.0                  # subscribe messages per second (the API limit is 50/s)


class ProbeError(RuntimeError):
    """The probe cannot run (no contracts to subscribe); the message says why."""


def recommend_lines(granted: int, hit_cap: bool, pool_exhausted: bool = False) -> Tuple[int, str]:
    """MARKET_DATA_LINES to configure and a sentence explaining it.

    90% of the granted lines leaves head-room for TWS watchlists and other API clients. When the
    cap was reached or the contract pool ran out IB never refused, so ``granted`` is only a lower
    bound.
    """
    lines = max(0, int(math.floor(granted * SAFETY_FACTOR)))
    if pool_exhausted:
        return lines, (f"IB granted at least {granted} lines (contract pool exhausted before "
                       f"IB refused any); the true allowance is higher. MARKET_DATA_LINES = {lines} "
                       f"is safe; the probe cannot measure more with the contracts IB listed.")
    if hit_cap:
        return lines, (f"IB granted at least {granted} lines (the cap was reached before IB refused "
                       f"any); the true allowance is higher. MARKET_DATA_LINES = {lines} is safe; "
                       f"raise --cap to measure more.")
    return lines, (f"IB granted exactly {granted} lines before refusing. "
                   f"Set MARKET_DATA_LINES = {lines} (90% head-room).")


def summarize(granted: int, refused: int, sent: int, cap: int) -> Tuple[int, str]:
    """Recommendation for a finished ramp: it reached the cap, ran out of contracts, or was refused."""
    hit_cap = refused == 0 and sent >= cap
    pool_exhausted = refused == 0 and not hit_cap
    return recommend_lines(granted, hit_cap, pool_exhausted)


async def _contract_pool(ib, cap: int) -> List:
    spx = _index_contract("SPX", "CBOE", "USD")
    spx_rows = await ib.req_contract_details(spx)
    if not spx_rows:
        raise ProbeError("no contract details for SPX (the lookup timed out or returned nothing): "
                         "check that TWS or the Gateway is up, logged in and allows API connections")
    spx.conId = spx_rows[0].contract.conId
    expirations, _ = await get_chain_params(ib, spx)
    pool: List = []
    for expiry in expirations[:8]:
        rows = await ib.req_chain_contract_details("SPX", expiry, "SPXW")
        pool += [d.contract for d in rows]
        await asyncio.sleep(1.0)
        if len(pool) >= cap + 50:
            break
    if not pool:
        raise ProbeError("IB listed no SPXW contracts to subscribe (no expirations or an empty "
                         "listing): try again when the security-definition farm is up")
    return pool


async def ramp(ib, pool: List, cap: int) -> Tuple[int, int, int]:
    """Subscribe up to ``cap`` contracts; return (granted, refused, sent)."""
    streams = []
    for i, contract in enumerate(pool[:cap]):
        streams.append(await ib.subscribe_tick_paced(contract, "", share="fixed"))
        if (i + 1) % 25 == 0:
            await asyncio.sleep(0.4)                       # let refusals arrive
            if any(s.failed_code == 101 for s in streams):
                break
    await asyncio.sleep(2.0)                                # trailing refusals
    refused = sum(1 for s in streams if s.failed_code == 101)
    return len(streams) - refused, refused, len(streams)


async def run(args) -> None:
    ib = IBClient(line_shares={"fixed": args.cap + 100, "order": 0, "poll": 0, "stream": 0},
                  pacer=RequestPacer(RAMP_RATE, 5))
    await ib.connect(args.host, args.port, args.client_id, timeout=15)
    try:
        pool = await _contract_pool(ib, args.cap)
        granted, refused, sent = await ramp(ib, pool, args.cap)
    finally:
        ib.disconnect()                                     # TWS drops every line of this client
    lines, text = summarize(granted, refused, sent, args.cap)
    print(f"contracts available: {len(pool)}, requests sent: {sent}")
    print(f"granted: {granted}, refused (error 101): {refused}, cap: {args.cap}")
    print(text)
    print(f"configured MARKET_DATA_LINES is {config.MARKET_DATA_LINES}")


def main() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default=config.IB_HOST)
    ap.add_argument("--port", type=int, default=config.IB_PORT)
    ap.add_argument("--client-id", type=int, default=140)
    ap.add_argument("--cap", type=int, default=2000, help="never ask for more lines than this")
    try:
        asyncio.run(run(ap.parse_args()))
    except ProbeError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
