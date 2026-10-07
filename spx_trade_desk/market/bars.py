"""
One-shot historical bar fetch (the chain poller's "no spot price yet" fallback) and annualized
volatility computation. The price chart's bars live in ``market/price_bars.py``.

All functions accept `ib` and `state` explicitly.
"""

import math
import logging
from datetime import datetime

from spx_trade_desk.market.hours import is_within_rth, last_trading_date, ET

logger = logging.getLogger(__name__)


async def compute_annual_vol(ib, state, lookback_days: int = 30) -> float:
    """Compute annualised realised volatility from IB daily close bars."""
    if state.spx_contract is None:
        return state.annual_vol

    try:
        bars = await ib.req_historical_bars(
            contract=state.spx_contract,
            end_date_time="",
            duration=f"{lookback_days} D",
            bar_size="1 day",
            what_to_show="TRADES",
            use_rth=True,
        )
    except Exception as e:
        logger.warning(f"Vol fetch failed, keeping {state.annual_vol:.1%}: {e}")
        return state.annual_vol

    if not bars or len(bars) < 5:
        logger.warning(f"Only {len(bars) if bars else 0} daily bars — not enough for vol calc")
        return state.annual_vol

    closes = [b.close for b in bars]
    log_returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    daily_std = (sum(r ** 2 for r in log_returns) / len(log_returns)) ** 0.5
    annual = daily_std * math.sqrt(252)
    state.annual_vol = annual
    logger.info(
        f"Realised vol computed from {len(log_returns)} daily bars: "
        f"daily σ={daily_std:.4f}, annual σ={annual:.2%}"
    )
    return annual


async def fetch_historical_bars(ib, state):
    """Fetch 1-min intraday bars for the last RTH session."""
    if state.spx_contract is None:
        return

    session_date = last_trading_date()

    if is_within_rth():
        end_dt = ""
    else:
        end_dt = datetime(
            session_date.year, session_date.month, session_date.day,
            16, 30, 0,
        ).strftime("%Y%m%d-%H:%M:%S")

    logger.info(f"Fetching 1-min historical bars for {session_date.isoformat()} "
                f"(endDateTime={'now' if end_dt == '' else end_dt})...")

    try:
        bars = await ib.req_historical_bars(
            contract=state.spx_contract,
            end_date_time=end_dt,
            duration="1 D",
            bar_size="1 min",
            what_to_show="TRADES",
            use_rth=True,
        )
    except Exception as e:
        logger.error(f"Historical bar fetch failed: {e}")
        return

    if not bars:
        logger.warning("No historical bars returned")
        return

    state.price_history.clear()
    for bar in bars:
        bar_dt = bar.date.astimezone(ET) if bar.date.tzinfo else bar.date.replace(tzinfo=ET)
        state.price_history.append({
            "time": bar_dt.isoformat(),
            "time_short": bar_dt.strftime("%H:%M"),
            "open": round(bar.open, 2),
            "high": round(bar.high, 2),
            "low": round(bar.low, 2),
            "close": round(bar.close, 2),
        })

    # The series' own session date: snapshots carry it and PriceBarFeed drops IB updates of another day.
    last_dt = bars[-1].date
    last_dt = last_dt.astimezone(ET) if last_dt.tzinfo else last_dt.replace(tzinfo=ET)
    state.price_session_date = last_dt.date().isoformat()

    last_close = bars[-1].close
    state.spx_price = last_close
    state.spx_last_close = last_close
    state.historical_date = session_date.isoformat()
    if state.data_mode != "live":
        state.data_mode = "historical"

    logger.info(
        f"Loaded {len(bars)} historical bars for {session_date.isoformat()}, "
        f"last close={last_close:.2f}"
    )
