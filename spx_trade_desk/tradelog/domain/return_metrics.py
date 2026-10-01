"""Time-weighted (TWR) and money-weighted (MWR / IRR) return calculations.

Pure computation — no Streamlit dependency — shared by the Streamlit UI and
the MCP server.  Both metrics measure the **SPX/SPXW strategy only**; every
non-SPX/SPXW transaction (stock trades, dividends, deposits/withdrawals,
non-SPX options) is treated as an *external cash flow* to/from the strategy
bucket, i.e. "outside" the return.

TWR
---
``compute_strategy_twr`` builds the strategy's value month-by-month from the
ledger::

    V(t) = initial + Σ(SPX/SPXW realized PnL up to t) + Σ(external flows up to t)

and links monthly returns ``r_m = Σ(SPX/SPXW PnL in month m) / V(month start)``:
``TWR = Π(1 + r_m) − 1``.  (``compute_twr`` also exists for statement-based
whole-account linking; it is not the SPX/SPXW-isolated method.)

MWR
---
``compute_mwr`` solves the internal rate of return over the **external**
flows only, *not* trade `net_amount`s.  Treating every trade as an external
flow double-counts the return in a self-contained account (there
``ending = initial + Σ net``, so the realized P&L would be discounted
twice).  The correct equation is::

    initial + Σ (flow_i / (1+r)^{Δt_i/365}) − ending / (1+r)^{T/365} = 0

where ``flow_i`` is the external cash flow into the account (positive =
deposit / stock sale / dividend; negative = stock buy / withdrawal).  With
no external flows this degenerates to the compounded simple return
``(ending / initial)^(365/T) − 1``.

The full transaction ledger is used separately for a *reconciliation* table
(initial → Σ SPX/SPXW PnL → Σ external flows → residual → ending).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

_DAYS_PER_YEAR = 365.0
_EPS = 1e-10
_MAX_BISECT = 200
_MAX_NEWTON = 100


@dataclass
class TwrPeriod:
    """One statement period (a month) used for time-weighted linking."""
    period_start: date
    beginning_value: float
    ending_value: float
    period_end: Optional[date] = None


def annualize(total_return: Optional[float], days: int) -> Optional[float]:
    """Annualize a total return over ``days`` calendar days."""
    if total_return is None or days is None or days <= 0:
        return None
    if total_return <= -1.0:
        return None
    return (1.0 + total_return) ** (_DAYS_PER_YEAR / days) - 1.0


def compute_twr(periods: list[TwrPeriod]) -> dict:
    """Compute monthly time-weighted return from statement periods.

    Returns a dict with ``twr`` (period TWR), ``annualized``, ``total_days``,
    ``period_count``, ``periods`` (per-period breakdown), and ``warnings``.
    Invalid periods (missing / non-positive beginning or ending value) are
    skipped with a warning.
    """
    warnings: list[str] = []
    valid: list[TwrPeriod] = []
    for p in sorted(periods, key=lambda p: p.period_start):
        if (
            p.beginning_value is None or p.ending_value is None
            or p.beginning_value <= 0 or p.ending_value <= 0
        ):
            warnings.append(
                f"Period {p.period_start}: invalid beginning/ending value — skipped"
            )
            continue
        valid.append(p)

    if not valid:
        return {
            "twr": None, "annualized": None, "total_days": 0, "period_count": 0,
            "periods": [], "warnings": warnings or ["No valid TWR periods."],
        }

    rows: list[dict] = []
    cumulative = 1.0
    for p in valid:
        r = p.ending_value / p.beginning_value - 1.0
        cumulative *= 1.0 + r
        rows.append({
            "period_start": p.period_start,
            "period_end": p.period_end,
            "beginning_value": p.beginning_value,
            "ending_value": p.ending_value,
            "monthly_return": r,
            "cumulative_return": cumulative - 1.0,
        })

    twr = cumulative - 1.0

    # Day span for annualization.  Use the last period's explicit end date
    # when available; otherwise approximate the final month as +30 days.
    last_end: Optional[date] = valid[-1].period_end
    if last_end is None:
        last_end = valid[-1].period_start + timedelta(days=30)
        warnings.append(
            "Last period has no explicit end date — approximated as +30 days "
            "for annualization."
        )
    total_days = (last_end - valid[0].period_start).days

    return {
        "twr": twr,
        "annualized": annualize(twr, total_days),
        "total_days": max(total_days, 0),
        "period_count": len(valid),
        "periods": rows,
        "warnings": warnings,
    }


def _strategy_monthly_rows(
    initial: float,
    spx_pnl_by_date: list[tuple[date, float]],
    external_flows: list[tuple[date, float]],
    statement_periods: list | None = None,
) -> tuple[list[dict], list[date]]:
    """Compute an account's monthly SPX/SPXW value series.

    Returns ``(rows, all_dates)``.  Each row covers one calendar month and
    carries ``y``, ``m``, ``period_start``, ``period_end``, ``beginning_value``,
    ``ending_value``, ``spx_pnl``, ``external_flows``, ``monthly_return`` (or
    ``None`` when the value at month start is non-positive), and
    ``cumulative_return``.

    A statement period anchors the month in which it **ends** (its ``ending
    value`` becomes that month's account value, so a spanning QFX with one
    ``period_end`` of July anchors July; a monthly E*Trade statement anchors
    its own month).  Months without an anchor are built from the ledger alone
    (``v_end = v_start + Σ SPX PnL + Σ external flows``).
    """
    from collections import defaultdict

    months: dict[tuple[int, int], dict[str, float]] = defaultdict(
        lambda: {"spx": 0.0, "flow": 0.0}
    )
    all_dates: list[date] = []
    for d, amt in spx_pnl_by_date:
        months[(d.year, d.month)]["spx"] += float(amt)
        all_dates.append(d)
    for d, amt in external_flows:
        months[(d.year, d.month)]["flow"] += float(amt)
        all_dates.append(d)

    # Statement-derived capital base, keyed by the month the statement ends in.
    end_date_by_month: dict[tuple[int, int], date] = {}
    end_value_by_month: dict[tuple[int, int], float] = {}
    if statement_periods:
        for p in statement_periods:
            if p.period_end is not None:
                key = (p.period_end.year, p.period_end.month)
            else:
                key = (p.period_start.year, p.period_start.month)
            end_date_by_month[key] = p.period_end
            end_value_by_month[key] = float(p.ending_value)

    if statement_periods:
        month_keys = sorted(set(months.keys()) | set(end_value_by_month.keys()))
    else:
        month_keys = sorted(months.keys())

    rows: list[dict] = []
    value = float(initial)
    cumulative = 1.0
    for (y, m) in month_keys:
        spx_m = months[(y, m)]["spx"]
        v_start = value
        if (y, m) in end_value_by_month:
            v_end = end_value_by_month[(y, m)]
            flows_m = v_end - v_start - spx_m  # implied external flows incl. deposits
        else:
            flows_m = months[(y, m)]["flow"]
            v_end = v_start + spx_m + flows_m

        if v_start > 0:
            r_m = spx_m / v_start
        else:
            r_m = None

        if r_m is not None:
            cumulative *= 1.0 + r_m

        value = v_end

        month_start = date(y, m, 1)
        month_end = end_date_by_month.get((y, m), None)
        if month_end is None:
            month_end = (
                (date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)) - timedelta(days=1)
            )
        rows.append({
            "y": y,
            "m": m,
            "period_start": month_start,
            "period_end": month_end,
            "beginning_value": v_start,
            "ending_value": v_end,
            "spx_pnl": spx_m,
            "external_flows": flows_m,
            "monthly_return": r_m,
            "cumulative_return": cumulative - 1.0 if r_m is not None else None,
        })
    return rows, all_dates


def compute_strategy_twr(
    initial: float,
    spx_pnl_by_date: list[tuple[date, float]],
    external_flows: list[tuple[date, float]],
    statement_periods: list | None = None,
) -> dict:
    """Monthly TWR of the SPX/SPXW strategy.

    The return driver is the SPX/SPXW realized PnL only.  The capital base the
    return is measured on is the *account's actual value*:

    * When ``statement_periods`` (monthly statements) are provided, the value
      at each month boundary is the statement's ending value, which includes
      deposits and every other non-SPX/SPXW movement.  The monthly return is
      ``Σ(SPX PnL in month) ÷ value at month start`` and the implied external
      flow (deposits/withdrawals, stock trades, dividends, mark-to-market) is
      ``ending − start − SPX PnL``.
    * Without statements, the value is built from the ledger alone
      (``initial + Σ SPX PnL + Σ external flows``), which understates the
      account when deposits are not present in the transaction file.

    Returns a dict with ``twr``, ``annualized``, ``total_days``,
    ``period_count``, ``periods`` (monthly breakdown), and ``warnings``.
    """
    rows, all_dates = _strategy_monthly_rows(
        initial, spx_pnl_by_date, external_flows, statement_periods
    )

    if not rows:
        return {
            "twr": None, "annualized": None, "total_days": 0, "period_count": 0,
            "periods": [], "warnings": ["No strategy activity to compute TWR from."],
        }

    warnings: list[str] = []
    cumulative = 1.0
    for row in rows:
        r_m = row["monthly_return"]
        if r_m is not None:
            cumulative *= 1.0 + r_m
        else:
            warnings.append(
                f"Month {row['y']}-{row['m']:02d}: value at month start <= 0 — return skipped."
            )

    valid_returns = [row["monthly_return"] for row in rows if row["monthly_return"] is not None]
    twr = cumulative - 1.0 if valid_returns else None

    if statement_periods:
        sps = sorted(statement_periods, key=lambda p: p.period_start)
        first_start = sps[0].period_start
        last_end = sps[-1].period_end or (sps[-1].period_start + timedelta(days=30))
        total_days = (last_end - first_start).days
    else:
        first_date, last_date = min(all_dates), max(all_dates)
        total_days = (last_date - first_date).days

    return {
        "twr": twr,
        "annualized": annualize(twr, total_days) if twr is not None else None,
        "total_days": max(total_days, 0),
        "period_count": len(valid_returns),
        "periods": rows,
        "warnings": warnings,
    }


def compute_portfolio_twr(accounts: list[dict]) -> dict:
    """Portfolio monthly TWR across multiple accounts.

    ``accounts`` is a list of dicts, each with ``initial``,
    ``spx_pnl_by_date``, ``external_flows_by_date`` and (optionally)
    ``statement_periods`` (a mix of ``TwrPeriod`` and ``EtradeBalance`` is
    fine — both duck-type ``.period_start`` / ``.period_end`` /
    ``.ending_value``).

    Each account's value is chained month-by-month (anchored where a statement
    snapshot exists, ledger-built elsewhere).  The portfolio value at each
    month boundary is the sum of the accounts' values, forward-filling an
    account's value across months where it has no activity or snapshot.  The
    monthly return is ``Σ SPX PnL ÷ Σ value at month start``.

    Returns the same dict shape as ``compute_strategy_twr``.
    """
    series: list[list[dict]] = []
    for acct in accounts:
        rows, _ = _strategy_monthly_rows(
            float(acct.get("initial") or 0.0),
            acct.get("spx_pnl_by_date") or [],
            acct.get("external_flows_by_date") or [],
            acct.get("statement_periods") or None,
        )
        series.append(rows)

    all_months = sorted({(r["y"], r["m"]) for rows in series for r in rows})

    if not all_months:
        return {
            "twr": None, "annualized": None, "total_days": 0, "period_count": 0,
            "periods": [], "warnings": ["No strategy activity to compute portfolio TWR from."],
        }

    indexed = [{ (r["y"], r["m"]): r for r in rows } for rows in series]
    last_value = [float(a.get("initial") or 0.0) for a in accounts]

    cumulative = 1.0
    rows_out: list[dict] = []
    warnings: list[str] = []
    valid_returns: list[float] = []

    for (y, m) in all_months:
        v_start_sum = 0.0
        v_end_sum = 0.0
        spx_sum = 0.0
        for k, ix in enumerate(indexed):
            row = ix.get((y, m))
            if row is not None:
                v_start_sum += row["beginning_value"]
                spx_sum += row["spx_pnl"]
                last_value[k] = row["ending_value"]
            else:
                v_start_sum += last_value[k]
            v_end_sum += last_value[k]
        flows_sum = v_end_sum - v_start_sum - spx_sum

        if v_start_sum > 0:
            r_m = spx_sum / v_start_sum
        else:
            r_m = None
            warnings.append(
                f"Month {y}-{m:02d}: portfolio value at month start <= 0 — return skipped."
            )
        if r_m is not None:
            cumulative *= 1.0 + r_m
            valid_returns.append(r_m)

        rows_out.append({
            "y": y,
            "m": m,
            "period_start": date(y, m, 1),
            "period_end": (
                (date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)) - timedelta(days=1)
            ),
            "beginning_value": v_start_sum,
            "ending_value": v_end_sum,
            "spx_pnl": spx_sum,
            "external_flows": flows_sum,
            "monthly_return": r_m,
            "cumulative_return": cumulative - 1.0 if r_m is not None else None,
        })

    twr = cumulative - 1.0 if valid_returns else None
    total_days = (rows_out[-1]["period_end"] - rows_out[0]["period_start"]).days
    return {
        "twr": twr,
        "annualized": annualize(twr, total_days) if twr is not None else None,
        "total_days": max(total_days, 0),
        "period_count": len(valid_returns),
        "periods": rows_out,
        "warnings": warnings,
    }


def compute_portfolio_mwr(accounts: list[dict], start: date, end: date) -> dict:
    """Portfolio money-weighted (IRR) return across accounts.

    ``accounts`` each carry ``initial``, ``ending`` and ``mwr_flows`` (a list
    of ``(date, amount)`` external flows, positive = deposit).  Combined
    initial / ending are summed; flows are merged by date; then ``compute_mwr``
    solves the portfolio IRR over the shared ``[start, end]`` window.
    """
    agg_initial = sum(float(a.get("initial") or 0.0) for a in accounts)
    agg_ending = sum(float(a.get("ending") or 0.0) for a in accounts)
    merged: dict[date, float] = {}
    for a in accounts:
        for d, amt in (a.get("mwr_flows") or []):
            merged[d] = merged.get(d, 0.0) + float(amt)
    return compute_mwr(agg_initial, agg_ending, start, end, cash_flows=list(merged.items()))


def statement_external_flows(
    initial: float,
    spx_pnl_by_date: list[tuple[date, float]],
    statement_periods: list,
) -> list[tuple[date, float]]:
    """Derive monthly external flows (incl. deposits/withdrawals) from statements.

    For each statement period the external flow is
    ``ending − value at start − SPX/SPXW PnL`` — the account's value change not
    explained by SPX/SPXW trading (deposits, withdrawals, stock trades,
    dividends, and mark-to-market).  SPX/SPXW PnL is summed over the period's
    own date range ``[period_start, period_end]`` (so a spanning QFX period
    subtracts the full window's PnL, not just the month it ends in).  Flows are
    dated at each period end.  Used for the MWR cash-flow stream.
    """
    flows: list[tuple[date, float]] = []
    prev_value = float(initial)
    for p in sorted(statement_periods, key=lambda p: p.period_start):
        p_end = p.period_end or (p.period_start + timedelta(days=30))
        spx_m = sum(
            float(amt) for d, amt in spx_pnl_by_date if p.period_start <= d <= p_end
        )
        flow = float(p.ending_value) - prev_value - spx_m
        flows.append((p_end, flow))
        prev_value = float(p.ending_value)
    return flows


# ---------------------------------------------------------------------------
# MWR / IRR
# ---------------------------------------------------------------------------

def _npv(r: float, initial: float, flows: list[tuple[float, float]],
         ending: float, t_end: float) -> float:
    """NPV of the external cash flows at annual rate ``r``.

    ``flows`` is a list of ``(t_years, amount)`` with ``amount > 0`` for a
    deposit (money into the account) and ``< 0`` for a withdrawal.
    """
    total = -initial
    for t_years, amount in flows:
        total -= amount / (1.0 + r) ** t_years
    total += ending / (1.0 + r) ** t_end
    return total


def _npv_deriv(r: float, flows: list[tuple[float, float]],
               ending: float, t_end: float) -> float:
    d = 0.0
    for t_years, amount in flows:
        d += amount * t_years * (1.0 + r) ** (-t_years - 1.0)
    d -= ending * t_end * (1.0 + r) ** (-t_end - 1.0)
    return d


def _solve_irr(initial: float, flows: list[tuple[float, float]],
               ending: float, t_end: float) -> Optional[float]:
    """Return the annual IRR solving NPV(r) = 0, or None if it fails to converge."""
    if t_end <= 0:
        return None

    def f(r: float) -> float:
        return _npv(r, initial, flows, ending, t_end)

    lo = -0.9999
    f_lo = f(lo)
    if abs(f_lo) < _EPS:
        return lo

    # Grow the high bracket until the sign changes.
    hi = 0.0
    f_hi = f(hi)
    if abs(f_hi) < _EPS:
        return hi
    if f_lo * f_hi > 0:
        for _ in range(60):
            hi = hi * 2.0 + 0.01
            f_hi = f(hi)
            if f_lo * f_hi <= 0 or hi > 1e6:
                break

    if f_lo * f_hi > 0:
        # No sign change in the bracket — try Newton from a few starting points.
        guesses = [0.0, 0.10, 0.25]
        simple = (ending / initial) ** (1.0 / t_end) - 1.0 if initial > 0 else None
        if simple is not None and simple > -0.9999:
            guesses.append(simple)
        for g in guesses:
            r = g
            for _ in range(_MAX_NEWTON):
                try:
                    fr = f(r)
                except (OverflowError, ZeroDivisionError, ValueError):
                    break
                if abs(fr) < _EPS:
                    return r
                dfr = _npv_deriv(r, flows, ending, t_end)
                if not math.isfinite(dfr) or abs(dfr) < _EPS:
                    break
                r_next = r - fr / dfr
                if r_next <= -0.9999 or not math.isfinite(r_next):
                    break
                if abs(r_next - r) < _EPS:
                    return r_next
                r = r_next
        return None

    # Bisection on [lo, hi].
    for _ in range(_MAX_BISECT):
        mid = 0.5 * (lo + hi)
        f_mid = f(mid)
        if abs(f_mid) < _EPS:
            return mid
        if f_lo * f_mid < 0:
            hi = mid
            f_hi = f_mid
        else:
            lo = mid
            f_lo = f_mid
    return 0.5 * (lo + hi)


def compute_mwr(
    initial: float,
    ending: float,
    start: date,
    end: date,
    cash_flows: Optional[list[tuple[date, float]]] = None,
) -> dict:
    """Compute the money-weighted (IRR) return over external cash flows.

    Parameters
    ----------
    initial:
        Account value at ``start`` (money the investor had in the account).
    ending:
        Account value at ``end``.
    start / end:
        Period boundaries (inclusive).
    cash_flows:
        Optional list of ``(date, amount)`` external flows.  ``amount > 0`` is
        a deposit into the account, ``amount < 0`` is a withdrawal.  Flows
        outside ``[start, end]`` are ignored.

    Returns
    -------
    dict
        ``mwr`` (return over the period), ``annualized`` (annualized rate),
        ``converged``, ``warning``, ``total_days``, and ``cf_table``.
    """
    total_days = (end - start).days
    t_end = total_days / _DAYS_PER_YEAR

    if initial is None or ending is None or initial <= 0 or ending <= 0:
        return {
            "mwr": None, "annualized": None, "converged": False,
            "warning": "Initial and ending capital must be positive.",
            "total_days": max(total_days, 0), "cf_table": [],
        }
    if total_days <= 0:
        return {
            "mwr": None, "annualized": None, "converged": False,
            "warning": "Period start must be before period end.",
            "total_days": 0, "cf_table": [],
        }

    flows: list[tuple[date, float, float, float]] = []  # (date, amount, t_years)
    cf_table: list[dict] = []
    for d, amount in (cash_flows or []):
        t_days = (d - start).days
        if t_days <= 0 or t_days > total_days:
            continue
        t_years = t_days / _DAYS_PER_YEAR
        flows.append((t_years, float(amount)))
        cf_table.append({
            "date": d,
            "amount": float(amount),
            "days_elapsed": t_days,
            "t_years": t_years,
        })

    if not flows:
        # Closed form: no external flows → compounded simple return.
        period_return = ending / initial - 1.0
        return {
            "mwr": period_return,
            "annualized": annualize(period_return, total_days),
            "converged": True,
            "warning": None,
            "total_days": total_days,
            "cf_table": [],
        }

    r = _solve_irr(initial, flows, ending, t_end)
    if r is None:
        return {
            "mwr": None, "annualized": None, "converged": False,
            "warning": "IRR did not converge (check initial / ending capital and cash flows).",
            "total_days": total_days, "cf_table": cf_table,
        }

    period_return = (1.0 + r) ** t_end - 1.0
    return {
        "mwr": period_return,
        "annualized": r,
        "converged": True,
        "warning": None,
        "total_days": total_days,
        "cf_table": cf_table,
    }
