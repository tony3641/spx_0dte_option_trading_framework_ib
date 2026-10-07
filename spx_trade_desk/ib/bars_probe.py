"""Read-only probe: how IB maintains SPX 1-minute bars with keepUpToDate (paper TWS only).

    python -m spx_trade_desk.ib.bars_probe [--port 7497] [--client-id 152] [--seconds 180]

Connects on its own client id, requests SPX index 1-minute TRADES bars with keepUpToDate, prints the
initial bars (count, first/last time) and, over ``--seconds``, how often updates arrive. Places no
orders. Run it in regular hours; outside them IB sends no updates.
"""
import argparse
import asyncio
import logging
import statistics
import sys
import time

from spx_trade_desk.core import config
from spx_trade_desk.ib.client import IBClient
from spx_trade_desk.ib.connection import _index_contract

LIVE_PORTS = (7496, 4001)               # standard TWS and Gateway live-account ports


def summarize(update_times, bars_by_minute) -> dict:
    """Update count, median/max gap between updates (seconds) and how many bar minutes were updated."""
    gaps = [b - a for a, b in zip(update_times, update_times[1:])]
    return {
        "updates": len(update_times),
        "median_gap_s": round(statistics.median(gaps), 3) if gaps else None,
        "max_gap_s": round(max(gaps), 3) if gaps else None,
        "minutes_updated": len(bars_by_minute),
    }


async def _run(host: str, port: int, client_id: int, seconds: float) -> int:
    ib = IBClient()
    await ib.connect(host, port, client_id)
    try:
        spx = _index_contract("SPX", "CBOE", "USD")       # built and qualified as setup_spx_subscription does
        details = await ib.req_contract_details(spx)
        if details:
            spx.conId = details[0].contract.conId
        times, minutes = [], {}

        def on_update(bar):
            times.append(time.monotonic())
            key = bar.date.strftime("%H:%M") if hasattr(bar.date, "strftime") else str(bar.date)
            minutes[key] = minutes.get(key, 0) + 1

        def on_error(code, msg):
            print(f"error {code}: {msg}")

        req_id, bars = await ib.req_historical_bars_live(spx, on_update, on_error)
        if not bars:
            ib.cancel_historical_bars(req_id)
            print("initial bars: 0 (no data or an error above): not waiting for updates")
            return 1
        print(f"initial bars: {len(bars)}  first={bars[0].date.isoformat()}  last={bars[-1].date.isoformat()}")
        await asyncio.sleep(seconds)
        ib.cancel_historical_bars(req_id)
        print(summarize(times, minutes))
        return 0
    finally:
        ib.disconnect()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default=config.IB_HOST)
    ap.add_argument("--port", type=int, default=7497, help="paper TWS 7497 (default) or paper Gateway 4002")
    ap.add_argument("--client-id", type=int, default=152)
    ap.add_argument("--seconds", type=float, default=180.0, help="how long to listen for updates")
    args = ap.parse_args(argv)
    if args.port in LIVE_PORTS:
        raise SystemExit(f"refusing to run: port {args.port} is a live-account port "
                         f"(paper TWS is 7497, paper Gateway 4002)")
    logging.basicConfig(level=logging.ERROR)              # IB errors on the request print via on_error
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        return asyncio.run(_run(args.host, args.port, args.client_id, args.seconds))
    except ConnectionError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
