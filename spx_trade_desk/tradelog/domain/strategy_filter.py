"""Strategy-row filtering: keep only SPX/SPXW option trades.

The dashboard's strategy / PnL views (equity curve, calendar, risk) are
SPX/SPXW-focused for **every** account.  Non-SPX/SPXW rows — stock trades,
dividends, fees, non-SPX options (e.g. NVDA/QCOM in an IBKR QFX) — stay in
the full ledger, which the money-weighted-return and reconciliation views
need for every cash movement, but they are excluded from the PnL views.

E*Trade accounts are identified by the virtual id ``"E*Trade"`` or the
``3-6-3`` digit shape of real Morgan Stanley / E*Trade account numbers;
IBKR ids start with ``U``.
"""

from __future__ import annotations

import re

import pandas as pd

from spx_trade_desk.tradelog.domain.parse_option_symbol import parse_occ_option_symbol
from spx_trade_desk.tradelog.io.load_etrade_csv import ETRADE_ACCOUNT_ID

# Underlyings the strategy is restricted to for E*Trade accounts.
STRATEGY_UNDERLYINGS = {"SPX", "SPXW"}

# Real E*Trade / Morgan Stanley account ids look like "999-999999-999".
_ETRADE_ACCT_PATTERN = re.compile(r"^\d{3}-\d{6}-\d{3}$")


def is_etrade_account(account_id) -> bool:
    """Return True if ``account_id`` belongs to an E*Trade account."""
    if account_id is None:
        return False
    acct = str(account_id)
    return acct == ETRADE_ACCOUNT_ID or bool(_ETRADE_ACCT_PATTERN.match(acct))


def is_strategy_symbol(symbol) -> bool:
    """Return True if a row's symbol is an SPX/SPXW option (the strategy)."""
    parsed = parse_occ_option_symbol(symbol)
    return parsed is not None and parsed.underlying in STRATEGY_UNDERLYINGS


def filter_strategy_rows(df: pd.DataFrame) -> pd.DataFrame:
    """Keep only SPX/SPXW option rows, for all accounts."""
    if df.empty:
        return df
    mask = df["symbol"].map(is_strategy_symbol).fillna(False)
    return df[mask].reset_index(drop=True)
