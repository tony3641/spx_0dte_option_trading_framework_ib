"""Focused tests for the ported pure-domain modules.

These pin the semantics the ported loaders depend on: the canonical contract key
format, OCC symbol padding, and the cross-file dedup rule (same-file duplicates
survive; only a later file's duplicate of an earlier file's key is dropped).
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from spx_trade_desk.tradelog.domain.merge import merge_transaction_frames
from spx_trade_desk.tradelog.domain.parse_option_symbol import (
    build_occ_symbol,
    parse_expiry_from_description,
    parse_occ_option_symbol,
)


def test_parse_occ_symbol_extracts_components():
    parsed = parse_occ_option_symbol("SPXW  260202P06940000")
    assert parsed is not None
    assert parsed.underlying == "SPXW"
    assert parsed.expiry_date == date(2026, 2, 2)
    assert parsed.right == "P"
    assert parsed.strike == 6940.0
    assert parsed.contract_key == "SPXW|2026-02-02|P|6940.000"


@pytest.mark.parametrize("bad", ["", "   ", "not-a-symbol", "SPXW260202X06940000"])
def test_parse_occ_symbol_rejects_bad_input(bad):
    assert parse_occ_option_symbol(bad) is None


def test_build_occ_symbol_pads_root_and_strike():
    assert build_occ_symbol("SPXW", date(2026, 2, 2), "P", 6940.0) == "SPXW  260202P06940000"
    assert build_occ_symbol("SPX", date(2026, 2, 2), "C", 6940.0) == "SPX   260202C06940000"


def test_build_occ_symbol_round_trips_through_parse():
    symbol = build_occ_symbol("SPXW", date(2026, 7, 15), "P", 7400.0)
    assert parse_occ_option_symbol(symbol).strike == 7400.0


def test_parse_expiry_from_description():
    assert parse_expiry_from_description("SPXW 15JUL26 7400 P") == date(2026, 7, 15)
    assert parse_expiry_from_description("no date here") is None


def _frame(rows):
    return pd.DataFrame(
        rows,
        columns=["activity_date", "account_id", "symbol", "quantity", "net_amount", "source_row"],
    )


def test_merge_keeps_same_file_duplicates_but_drops_cross_file_duplicates():
    shared = {
        "activity_date": date(2026, 1, 15),
        "account_id": "U1",
        "symbol": "SPXW  260115P06700000",
        "quantity": 1.0,
        "net_amount": 499.35,
    }
    frame_a = _frame([
        {**shared, "source_row": 1},
        {**shared, "source_row": 2},   # same-file duplicate: a distinct fill, kept
    ])
    frame_b = _frame([
        {**shared, "source_row": 1},   # duplicates frame_a's key: dropped
        {
            "activity_date": date(2026, 1, 16),
            "account_id": "U1",
            "symbol": "SPXW  260116P06800000",
            "quantity": 2.0,
            "net_amount": 698.70,
            "source_row": 2,
        },
    ])

    merged = merge_transaction_frames([frame_a, frame_b])

    assert len(merged) == 3
    assert "_src" not in merged.columns
    assert list(merged["source_row"]) == [1, 2, 3]
    assert list(merged["activity_date"]) == sorted(merged["activity_date"])


def test_merge_with_single_frame_never_dedups():
    frame = _frame([
        {
            "activity_date": date(2026, 1, 15),
            "account_id": "U1",
            "symbol": "SPXW  260115P06700000",
            "quantity": 1.0,
            "net_amount": 499.35,
            "source_row": 1,
        },
        {
            "activity_date": date(2026, 1, 15),
            "account_id": "U1",
            "symbol": "SPXW  260115P06700000",
            "quantity": 1.0,
            "net_amount": 499.35,
            "source_row": 2,
        },
    ])
    assert len(merge_transaction_frames([frame])) == 2
