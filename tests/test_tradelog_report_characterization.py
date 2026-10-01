"""Characterization of the QFX monthly report.

Written BEFORE ``build_report`` was refactored to accept a trade-log workbook, so the refactor can be proven
behaviour-neutral. The snapshot pins the HTML (footer timestamp normalised) and ``report_data`` of the repo's
synthetic QFX with deterministic, faked market data, so a refreshed ``reports/data`` cache cannot change it.

Regenerate deliberately, only when the QFX report is meant to change:
    python -c "from tests.test_tradelog_report_characterization import write_golden; write_golden()"
"""
from __future__ import annotations

import hashlib
import json
import re
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pandas as pd
import pytest

from spx_trade_desk.tradelog.analysis import strategy_analysis as sa
from spx_trade_desk.tradelog.report.generate_report import build_report
from tests.test_tradelog_analysis import SYNTHETIC_QFX

GOLDEN = Path(__file__).parent / "fixtures" / "report_qfx_golden.json"


def fake_market_data(spx_csv, vix_csv, start_year, end_date, offline=False):
    """Deterministic SPX/VIX frames covering Jan 1 of ``start_year`` through ``end_date``."""
    days = pd.bdate_range(f"{start_year}-01-01", pd.Timestamp(end_date))
    dates = [d.strftime("%Y-%m-%d") for d in days]
    n = len(dates)
    spx = pd.DataFrame({
        "activity_date": dates,
        "spx_close": [5000.0 + 3.0 * i + 7.0 * (i % 5) for i in range(n)],
    })
    spx["spx_return"] = spx["spx_close"].pct_change()
    vix_close = [15.0 + 0.2 * (i % 7) for i in range(n)]
    vix = pd.DataFrame({
        "activity_date": dates,
        "vix_open": [v - 0.1 for v in vix_close],
        "vix_high": [v + 0.4 for v in vix_close],
        "vix_low": [v - 0.4 for v in vix_close],
        "vix_close": vix_close,
    })
    vix["vix_change"] = vix["vix_close"].diff()
    return spx, vix


def render_qfx_report(qfx_path):
    args = SimpleNamespace(monthly=str(qfx_path), month=None, ytd=None, rf=0.04,
                           label="Synthetic Test", offline=True)
    with mock.patch.object(sa, "load_market_data", fake_market_data):
        return build_report(args)


def snapshot(html_doc, report_data) -> dict:
    normalised = re.sub(r"Generated \d{4}-\d{2}-\d{2} \d{2}:\d{2}", "Generated <ts>", html_doc)
    return {
        "html_sha256": hashlib.sha256(normalised.encode("utf-8")).hexdigest(),
        "section_numbers": re.findall(r'<span class="sec-num">(\d+)</span>', html_doc),
        "report_data": json.loads(json.dumps(report_data, sort_keys=True)),
    }


def write_golden():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "synthetic.qfx"
        path.write_text(SYNTHETIC_QFX, encoding="latin-1")
        GOLDEN.write_text(
            json.dumps(snapshot(*render_qfx_report(path)), indent=2, sort_keys=True),
            encoding="utf-8",
        )


@pytest.fixture
def qfx_path(tmp_path):
    path = tmp_path / "synthetic.qfx"
    path.write_text(SYNTHETIC_QFX, encoding="latin-1")
    return path


def test_qfx_report_is_unchanged(qfx_path):
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert snapshot(*render_qfx_report(qfx_path)) == golden
