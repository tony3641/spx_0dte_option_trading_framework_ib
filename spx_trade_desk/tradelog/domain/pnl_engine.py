from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

import pandas as pd

from spx_trade_desk.tradelog.domain.parse_option_symbol import parse_expiry_from_description, parse_occ_option_symbol


PNL_EXCLUDED_TYPES = {"Other Fee"}


@dataclass
class PnlResult:
    enriched_rows: pd.DataFrame
    daily: pd.DataFrame


def _derive_option_fields(df: pd.DataFrame) -> pd.DataFrame:
    # If already enriched, avoid re-deriving (which would duplicate columns)
    if "contract_key" in df.columns and "is_option" in df.columns:
        return df.reset_index(drop=True)

    rows = []
    for _, row in df.iterrows():
        parsed = parse_occ_option_symbol(row["symbol"])
        if parsed is not None:
            rows.append(
                {
                    "contract_key": parsed.contract_key,
                    "underlying": parsed.underlying,
                    "expiry_date": parsed.expiry_date,
                    "right": parsed.right,
                    "strike": parsed.strike,
                    "is_option": True,
                }
            )
            continue

        expiry_fallback: Optional[date] = parse_expiry_from_description(row["description"])
        rows.append(
            {
                "contract_key": None,
                "underlying": None,
                "expiry_date": expiry_fallback,
                "right": None,
                "strike": None,
                "is_option": expiry_fallback is not None,
            }
        )

    option_df = pd.DataFrame(rows)
    return pd.concat([df.reset_index(drop=True), option_df], axis=1)


def _mark_expire_inferred(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["is_expire_inferred"] = False

    candidates = df[
        (df["transaction_type"].str.lower() == "sell")
        & (df["is_option"])
        & (df["quantity"] < 0)
        & (df["activity_date"] == df["expiry_date"])
        & (df["contract_key"].notna())
    ]

    for idx, row in candidates.iterrows():
        same_contract_day = df[
            (df["account_id"] == row["account_id"])
            & (df["activity_date"] == row["activity_date"])
            & (df["contract_key"] == row["contract_key"])
        ]

        has_buyback = (
            (same_contract_day["transaction_type"].str.lower() == "buy")
            & (same_contract_day["quantity"] > 0)
        ).any()

        has_cash_settlement = same_contract_day["transaction_type"].str.contains(
            "Cash Settlement", case=False, na=False
        ).any()

        if not has_buyback and not has_cash_settlement:
            df.at[idx, "is_expire_inferred"] = True

    return df


def build_realized_pnl(df: pd.DataFrame) -> PnlResult:
    enriched = _derive_option_fields(df)
    enriched = _mark_expire_inferred(enriched)

    enriched["transaction_type"] = enriched["transaction_type"].fillna("")
    enriched["commission"] = enriched["commission"].fillna(0.0)
    enriched["net_amount"] = enriched["net_amount"].fillna(0.0)
    enriched["quantity"] = enriched["quantity"].fillna(0.0)
    enriched["in_pnl"] = ~enriched["transaction_type"].isin(PNL_EXCLUDED_TYPES)
    enriched["option_contracts_traded"] = 0
    option_mask = enriched["is_option"]
    enriched.loc[option_mask, "option_contracts_traded"] = (
        enriched.loc[option_mask, "quantity"].abs().astype(int)
    )
    enriched["expire_inferred_contracts"] = 0
    inferred_mask = enriched["is_expire_inferred"]
    enriched.loc[inferred_mask, "expire_inferred_contracts"] = (
        enriched.loc[inferred_mask, "quantity"].abs().astype(int)
    )

    enriched["realization_reason"] = "close_trade"
    enriched.loc[enriched["is_expire_inferred"], "realization_reason"] = "expire_inferred"
    enriched.loc[
        enriched["transaction_type"].str.contains("Cash Settlement", case=False, na=False),
        "realization_reason",
    ] = "cash_settlement"

    daily = (
        enriched.groupby("activity_date", as_index=False)
        .agg(
            realized_pnl=("net_amount", lambda s: s[enriched.loc[s.index, "in_pnl"]].sum()),
            commission_spent=("commission", lambda s: s[enriched.loc[s.index, "in_pnl"]].abs().sum()),
            option_contracts_traded=("option_contracts_traded", "sum"),
            trade_count=("source_row", "count"),
            expire_inferred_count=("is_expire_inferred", "sum"),
            expire_inferred_contract_count=("expire_inferred_contracts", "sum"),
            expire_inferred_pnl=(
                "net_amount",
                lambda s: s[enriched.loc[s.index, "is_expire_inferred"]].sum(),
            ),
        )
        .sort_values("activity_date")
        .reset_index(drop=True)
    )

    daily["cumulative_pnl"] = daily["realized_pnl"].cumsum()
    return PnlResult(enriched_rows=enriched, daily=daily)
