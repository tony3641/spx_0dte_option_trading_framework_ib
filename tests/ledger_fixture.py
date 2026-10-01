"""Invented trade-log workbook for the report tests.

Every value is made up: fictional tickers, round prices, a placeholder account id. Never copy anything from
a real statement or trade log into this file.
"""
from __future__ import annotations

from datetime import date

from openpyxl import Workbook

HEADER = [
    "Date", "Source", "Account", "Underlying", "Symbol", "Description",
    "Order Type", "Quantity", "Price", "Commission", "Net Amount",
]
IBKR = ("IBKR", "U***12345")
ETRADE = ("ETRADE", "ET-ACCT-1")

# One SPXW put spread per day. IBKR rows: OCC symbol, positive qty, signed net, negative commission.
# E*Trade rows: non-OCC symbol, option detail in Description. 2026-07-06 holds a short bought back the
# same day (closed intraday).
SPX_ROWS = [
    (date(2026, 6, 1), *IBKR, "SPXW", "SPXW  260601P05000000", "SPXW 01JUN26 5000 P", "Sell", 1, 0.50, -1.00, 49.00),
    (date(2026, 6, 1), *IBKR, "SPXW", "SPXW  260601P04950000", "SPXW 01JUN26 4950 P", "Buy", 1, 0.20, -1.00, -21.00),
    (date(2026, 6, 2), *IBKR, "SPXW", "SPXW  260602P05000000", "SPXW 02JUN26 5000 P", "Sell", 1, 0.60, -1.00, 59.00),
    (date(2026, 6, 2), *IBKR, "SPXW", "SPXW  260602P04950000", "SPXW 02JUN26 4950 P", "Buy", 1, 0.30, -1.00, -31.00),
    (date(2026, 6, 3), *IBKR, "SPXW", "SPXW  260603P05000000", "SPXW 03JUN26 5000 P", "Sell", 1, 0.40, -1.00, 39.00),
    (date(2026, 6, 3), *IBKR, "SPXW", "SPXW  260603P04950000", "SPXW 03JUN26 4950 P", "Buy", 1, 0.50, -1.00, -51.00),
    (date(2026, 6, 2), *ETRADE, "SPXW", "SPXW JUN 02 '26 $5100 PUT", "PUT  SPXW   06/02/26  5100.000",
     "Sell To Open", 2, 0.40, -1.00, 78.00),
    (date(2026, 6, 2), *ETRADE, "SPXW", "SPXW JUN 02 '26 $5050 PUT", "PUT  SPXW   06/02/26  5050.000",
     "Buy Open", 2, 0.10, -1.00, -21.00),
    (date(2026, 7, 1), *IBKR, "SPXW", "SPXW  260701P05000000", "SPXW 01JUL26 5000 P", "Sell", 1, 0.50, -1.00, 49.00),
    (date(2026, 7, 1), *IBKR, "SPXW", "SPXW  260701P04950000", "SPXW 01JUL26 4950 P", "Buy", 1, 0.20, -1.00, -21.00),
    (date(2026, 7, 2), *IBKR, "SPXW", "SPXW  260702P05000000", "SPXW 02JUL26 5000 P", "Sell", 1, 0.60, -1.00, 59.00),
    (date(2026, 7, 2), *IBKR, "SPXW", "SPXW  260702P04950000", "SPXW 02JUL26 4950 P", "Buy", 1, 0.30, -1.00, -31.00),
    (date(2026, 7, 6), *IBKR, "SPXW", "SPXW  260706P05000000", "SPXW 06JUL26 5000 P", "Sell", 1, 0.40, -1.00, 39.00),
    (date(2026, 7, 6), *IBKR, "SPXW", "SPXW  260706P05000000", "SPXW 06JUL26 5000 P", "Buy", 1, 0.80, -1.00, -81.00),
    (date(2026, 7, 6), *IBKR, "SPXW", "SPXW  260706P04950000", "SPXW 06JUL26 4950 P", "Buy", 1, 0.10, -1.00, -11.00),
]
# Outside the strategy: another underlying, a stock, a dividend, a fee. The report must ignore them.
OTHER_OPTION_ROWS = [
    (date(2026, 6, 1), *IBKR, "ABCD", "ABCD  260605P00100000", "ABCD 05JUN26 100 P", "Sell", 3, 0.50, -1.50, 148.50),
]
STOCK_ROWS = [
    (date(2026, 6, 4), *ETRADE, "ACME", "ACME", "ACME CORP UNSOLICITED TRADE", "Buy", 2, 100, 0, -200.00),
]
OTHER_TX_ROWS = [
    (date(2026, 6, 5), *IBKR, "FUND", "FUND", "FUND Cash Dividend USD 0.25 per Share", "Dividend",
     None, None, None, 100.00),
    (date(2026, 6, 4), *IBKR, None, "-", "Monthly market data fee", "Other Fee", None, None, None, -10.00),
]

# Realized P&L per SPX/SPXW trading day, in date order, and the monthly totals.
DAILY_PNL = [28.0, 85.0, -12.0, 28.0, 28.0, -53.0]
JUNE_PNL = 101.0
JULY_PNL = 3.0
TOTAL_PNL = 104.0


def write_ledger(path, spx_rows=None, other_tabs=True):
    """Write the four-tab workbook to ``path`` and return it."""
    wb = Workbook()
    tabs = [("Index Options", SPX_ROWS if spx_rows is None else spx_rows)]
    if other_tabs:
        tabs += [("Other Options", OTHER_OPTION_ROWS), ("Stock & ETF", STOCK_ROWS),
                 ("Other Transactions", OTHER_TX_ROWS)]
    for i, (title, rows) in enumerate(tabs):
        ws = wb.active if i == 0 else wb.create_sheet()
        ws.title = title
        ws.append(HEADER)
        for row in rows:
            ws.append(list(row))
    wb.save(path)
    return path


def day_pnl_rows(day, pnl):
    """Two IBKR rows (a short sale and a protective buy at another strike) that net to ``pnl`` for ``day``."""
    stamp = day.strftime("%y%m%d")
    buy_price = (48.0 - pnl) / 100.0  # sell 1 @ 0.50 nets 49.00; buy 1 @ p nets -(100 p + 1)
    return [
        (day, *IBKR, "SPXW", f"SPXW  {stamp}P05000000", f"SPXW {stamp} 5000 P", "Sell", 1, 0.50, -1.00, 49.00),
        (day, *IBKR, "SPXW", f"SPXW  {stamp}P04950000", f"SPXW {stamp} 4950 P", "Buy", 1, round(buy_price, 4),
         -1.00, round(-(100.0 * buy_price + 1.0), 2)),
    ]
