from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, date
from typing import Optional


OCC_PATTERN = re.compile(r"^\s*([A-Z.\-]+)\s*(\d{6})([CP])(\d{8})\s*$")
DESC_EXP_PATTERN = re.compile(r"\b(\d{1,2}[A-Z]{3}\d{2})\b")


@dataclass(frozen=True)
class ParsedOption:
    underlying: str
    expiry_date: date
    right: str
    strike: float
    contract_key: str


def parse_occ_option_symbol(symbol: str) -> Optional[ParsedOption]:
    if not isinstance(symbol, str) or not symbol.strip():
        return None

    match = OCC_PATTERN.match(symbol)
    if not match:
        return None

    underlying = match.group(1).strip()
    expiry_raw = match.group(2)
    right = match.group(3)
    strike_raw = match.group(4)

    expiry_date = datetime.strptime(expiry_raw, "%y%m%d").date()
    strike = int(strike_raw) / 1000.0
    contract_key = f"{underlying}|{expiry_date.isoformat()}|{right}|{strike:.3f}"

    return ParsedOption(
        underlying=underlying,
        expiry_date=expiry_date,
        right=right,
        strike=strike,
        contract_key=contract_key,
    )


def build_occ_symbol(
    underlying: str, expiry: date, right: str, strike: float,
) -> str:
    """
    Build a padded OCC option symbol (inverse of parse_occ_option_symbol).

    Example: build_occ_symbol("SPXW", date(2026,2,2), "P", 6940.0)
             → "SPXW  260202P06940000"
    """
    root = underlying.upper().ljust(6)
    expiry_str = expiry.strftime("%y%m%d")
    right_char = "P" if right.upper().startswith("P") else "C"
    strike_int = int(round(strike * 1000))
    return f"{root}{expiry_str}{right_char}{strike_int:08d}"


def parse_expiry_from_description(description: str) -> Optional[date]:
    if not isinstance(description, str):
        return None

    match = DESC_EXP_PATTERN.search(description.upper())
    if not match:
        return None

    try:
        return datetime.strptime(match.group(1), "%d%b%y").date()
    except ValueError:
        return None
