"""build_report on a trade-log workbook (day-level report) and the QFX-only notices."""
from __future__ import annotations

import base64
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from spx_trade_desk.mcp.server import generate_monthly_report
from spx_trade_desk.resources import REPORT_OUTPUT_DIR
from spx_trade_desk.tradelog.analysis import strategy_analysis as sa
from spx_trade_desk.tradelog.report.generate_report import build_report
from tests.ledger_fixture import JULY_PNL, JUNE_PNL, SPX_ROWS, TOTAL_PNL, day_pnl_rows, write_ledger
from tests.test_tradelog_analysis import SYNTHETIC_QFX
from tests.test_tradelog_report_characterization import fake_market_data


@pytest.fixture(autouse=True)
def fake_market(monkeypatch):
    monkeypatch.setattr(sa, "load_market_data", fake_market_data)


@pytest.fixture
def ledger_path(tmp_path):
    return str(write_ledger(tmp_path / "trade_log.xlsx"))


def _args(path, **over):
    base = dict(monthly=path, month=None, ytd=None, rf=0.04, label=None, offline=True)
    base.update(over)
    return SimpleNamespace(**base)


def _sections(html_doc):
    return re.findall(r'<span class="sec-num">(\d+)</span>', html_doc)


class TestWholeWorkbook:
    def test_totals_come_from_the_spx_spxw_rows(self, ledger_path):
        _html, data = build_report(_args(ledger_path))
        assert data["total_pnl"] == pytest.approx(TOTAL_PNL)
        assert data["trading_days"] == 6
        assert data["positive_days"] == 4 and data["negative_days"] == 2
        assert data["initial_capital"] == 100_000.0
        json.dumps(data)  # must stay JSON-serializable

    def test_window_without_a_month_is_titled_with_the_span(self, ledger_path):
        html_doc, data = build_report(_args(ledger_path))
        assert data["month"] == "2026-06 → 2026-07"
        assert "June 2026 – July 2026" in html_doc

    def test_all_twelve_sections_stay_in_place(self, ledger_path):
        html_doc, _data = build_report(_args(ledger_path))
        assert _sections(html_doc) == [str(n) for n in range(1, 13)]

    def test_direction_dependent_sections_show_a_notice_instead_of_numbers(self, ledger_path):
        html_doc, data = build_report(_args(ledger_path))
        assert html_doc.count("Needs QFX timestamps") == 3
        assert "Short leg (the bet)" not in html_doc
        assert "Binary Kelly" not in html_doc
        assert "Notable positions" not in html_doc
        assert data["unavailable_sections"] == [7, 9, 11]
        assert data["spreads"] is None and data["stops"] is None and data["kelly"] is None
        assert data["tail"]["gap_stress"] is None

    def test_day_level_sections_are_present(self, ledger_path):
        html_doc, data = build_report(_args(ledger_path))
        assert data["edge"]["ci"] and data["edge"]["p_value"] is not None
        assert data["tail"]["monte_carlo"]
        assert "Day-level EV" in html_doc

    def test_provenance_box_states_source_capital_and_coverage(self, ledger_path):
        html_doc, data = build_report(_args(ledger_path))
        assert "About this report" in html_doc
        assert "trade-log workbook (dates only, no intraday timestamps)" in html_doc
        assert "assumed" in html_doc
        assert "14 contract-days, 1 closed within the same day" in html_doc
        assert data["source"]["capital_assumed"] is True
        assert data["source"]["contract_days"] == 14

    def test_footer_does_not_claim_a_qfx_statement(self, ledger_path):
        html_doc, _data = build_report(_args(ledger_path))
        assert "computed from your trade-log workbook" in html_doc
        assert "computed from your QFX" not in html_doc


class TestMonthSlice:
    def test_july_starts_from_capital_plus_prior_pnl(self, ledger_path):
        _html, data = build_report(_args(ledger_path, month="2026-07"))
        assert data["total_pnl"] == pytest.approx(JULY_PNL)
        assert data["initial_capital"] == pytest.approx(100_000.0 + JUNE_PNL)
        assert data["month"] == "2026-07"

    def test_earlier_months_feed_cross_month_and_pooled_significance(self, ledger_path):
        _html, data = build_report(_args(ledger_path, month="2026-07"))
        assert [row["month"] for row in data["cross_month"]] == ["2026-06", "2026-07"]
        assert data["cross_month"][0]["pnl"] == pytest.approx(JUNE_PNL)
        assert data["edge"]["pooled"]["days"] == 6

    def test_first_month_has_no_prior_context(self, ledger_path):
        _html, data = build_report(_args(ledger_path, month="2026-06"))
        assert [row["month"] for row in data["cross_month"]] == ["2026-06"]
        assert data["edge"]["pooled"] is None
        assert data["initial_capital"] == 100_000.0

    def test_supplied_capital_is_used_and_not_called_assumed(self, ledger_path):
        html_doc, data = build_report(_args(ledger_path, initial_capital=50_000))
        assert data["initial_capital"] == 50_000.0
        assert data["source"]["capital_assumed"] is False
        assert "supplied" in html_doc


class TestAccountFilter:
    def test_single_account_totals_and_label(self, ledger_path):
        html_doc, data = build_report(_args(ledger_path, account="E*Trade"))
        assert data["total_pnl"] == pytest.approx(57.0)
        assert data["trading_days"] == 1
        assert "Account E*Trade" in html_doc

    def test_unknown_account_is_a_clear_error(self, ledger_path):
        with pytest.raises(ValueError, match="NOPE"):
            build_report(_args(ledger_path, account="NOPE"))


class TestEdges:
    def test_workbook_without_spx_rows_reports_no_trades(self, tmp_path):
        path = write_ledger(tmp_path / "stocks_only.xlsx", spx_rows=[])
        html_doc, data = build_report(_args(str(path)))
        assert data is None and "No trades found" in html_doc

    def test_month_without_trades_reports_no_trades(self, ledger_path):
        html_doc, data = build_report(_args(ledger_path, month="2026-08"))
        assert data is None and "No trades found" in html_doc

    def test_single_trading_day_window_renders(self, tmp_path):
        path = write_ledger(tmp_path / "one_day.xlsx", spx_rows=SPX_ROWS[:2], other_tabs=False)
        html_doc, data = build_report(_args(str(path)))
        assert data["trading_days"] == 1
        assert data["total_pnl"] == pytest.approx(28.0)
        assert _sections(html_doc) == [str(n) for n in range(1, 13)]

    def test_uppercase_extension_builds_a_report(self, tmp_path):
        path = write_ledger(tmp_path / "TRADE_LOG.XLSX")
        _html, data = build_report(_args(str(path)))
        assert data["total_pnl"] == pytest.approx(TOTAL_PNL)

    def test_nonpositive_capital_is_a_clear_error(self, ledger_path):
        with pytest.raises(ValueError, match="initial_capital"):
            build_report(_args(ledger_path, initial_capital=0))

    def test_ytd_with_a_workbook_is_a_clear_error(self, ledger_path):
        with pytest.raises(ValueError, match="ytd"):
            build_report(_args(ledger_path, ytd="earlier.qfx"))

    def test_cli_style_namespace_without_the_new_attributes_still_works(self, ledger_path):
        args = SimpleNamespace(monthly=ledger_path, month=None, ytd=None, rf=0.04, label=None, offline=True)
        _html, data = build_report(args)
        assert data["total_pnl"] == pytest.approx(TOTAL_PNL)

    def test_build_report_writes_no_file(self, ledger_path):
        before = set(REPORT_OUTPUT_DIR.glob("*.html"))
        build_report(_args(ledger_path))
        assert set(REPORT_OUTPUT_DIR.glob("*.html")) == before



class TestTool:
    @pytest.fixture(autouse=True)
    def isolated_output_dir(self, tmp_path, monkeypatch):
        """The tool writes its HTML report; keep it out of the repo's real reports/output."""
        monkeypatch.setattr("spx_trade_desk.mcp.server.REPORT_OUTPUT_DIR", tmp_path / "reports_out")

    def test_by_path(self, ledger_path):
        result = generate_monthly_report(paths=[ledger_path], label="Workbook Test", offline=True)
        assert "error" not in result
        assert result["total_pnl"] == pytest.approx(TOTAL_PNL)
        assert result["unavailable_sections"] == [7, 9, 11]
        assert result["html_path"].endswith(".html")
        json.dumps(result)

    def test_by_base64_content(self, ledger_path):
        with open(ledger_path, "rb") as fh:
            payload = base64.b64encode(fh.read()).decode()
        result = generate_monthly_report(
            file_contents=[{"name": "ledger.xlsx", "data_base64": payload}],
            label="Workbook Base64 Test", offline=True)
        assert result["total_pnl"] == pytest.approx(TOTAL_PNL)

    def test_month_account_and_capital_parameters(self, ledger_path):
        result = generate_monthly_report(
            paths=[ledger_path], month="2026-07", account_filter="U***12345",
            initial_capital=50_000, label="Workbook Params Test", offline=True)
        assert result["total_pnl"] == pytest.approx(JULY_PNL)
        assert result["initial_capital"] == pytest.approx(50_000 + 44.0)  # IBKR-only June P&L

    def test_csv_is_rejected_with_a_clear_message_and_no_traceback(self, tmp_path):
        path = tmp_path / "statement.csv"
        path.write_text("Date,Account\n")
        result = generate_monthly_report(paths=[str(path)], offline=True)
        assert "statement.csv" in result["error"] and "QFX" in result["error"] and ".xlsx" in result["error"]
        assert "traceback" not in result

    def test_unsupported_upload_is_named_by_its_own_name(self):
        result = generate_monthly_report(
            file_contents=[{"name": "my_statement.pdf", "data_base64": base64.b64encode(b"x").decode()}],
            offline=True)
        assert "my_statement.pdf" in result["error"]

    def test_ytd_with_a_workbook_is_an_error(self, ledger_path, tmp_path):
        qfx = tmp_path / "earlier.qfx"
        qfx.write_text(SYNTHETIC_QFX, encoding="latin-1")
        result = generate_monthly_report(paths=[ledger_path], ytd_paths=[str(qfx)], offline=True)
        assert "ytd" in result["error"].lower()

    def test_unknown_account_is_an_error_listing_the_available_ones(self, ledger_path):
        result = generate_monthly_report(paths=[ledger_path], account_filter="NOPE", offline=True)
        assert "NOPE" in result["error"] and "U***12345" in result["error"]

    def test_nonpositive_capital_is_an_error(self, ledger_path):
        result = generate_monthly_report(paths=[ledger_path], initial_capital=0, offline=True)
        assert "initial_capital" in result["error"]

    def test_workbook_without_spx_rows_is_a_no_trades_error(self, tmp_path):
        path = write_ledger(tmp_path / "stocks_only.xlsx", spx_rows=[])
        result = generate_monthly_report(paths=[str(path)], offline=True)
        assert "No trades found" in result["error"]

    def test_qfx_ignores_capital_and_account_with_warnings(self, tmp_path):
        qfx = tmp_path / "statement.qfx"
        qfx.write_text(SYNTHETIC_QFX, encoding="latin-1")
        result = generate_monthly_report(
            paths=[str(qfx)], initial_capital=1.0, account_filter="U123456",
            label="Qfx Params Test", offline=True)
        assert result["total_pnl"] == pytest.approx(28.70, abs=0.01)
        assert any("initial_capital" in w for w in result["warnings"])
        assert any("account_filter" in w for w in result["warnings"])


def _two_month_workbook(tmp_path, june, july):
    rows = []
    for i, pnl in enumerate(june):
        rows += day_pnl_rows(date(2026, 6, 1 + i), pnl)
    for i, pnl in enumerate(july):
        rows += day_pnl_rows(date(2026, 7, 1 + i), pnl)
    return str(write_ledger(tmp_path / "two_months.xlsx", spx_rows=rows, other_tabs=False))


SIGNIFICANT_SENTENCE = "the edge is statistically significant when measured across the full window"


class TestPooledWording:
    """The pooled paragraph and takeaway must follow the pooled numbers, not assume a significant edge."""

    def test_losing_pooled_window_is_not_called_significant_or_positive(self, tmp_path):
        path = _two_month_workbook(tmp_path, [-100, -80, 10, -120, -60], [-30, 10, -20])
        html_doc, data = build_report(_args(path, month="2026-07"))
        assert data["edge"]["pooled"]["ev"] < 0
        assert SIGNIFICANT_SENTENCE not in html_doc
        assert "still positive" not in html_doc
        assert "no edge is demonstrated" in html_doc

    def test_positive_but_noisy_pooled_window_is_not_called_significant(self, tmp_path):
        path = _two_month_workbook(tmp_path, [30, -40, 35, -35, 30], [20, -30, 25])
        html_doc, data = build_report(_args(path, month="2026-07"))
        assert data["edge"]["pooled"]["ev"] > 0 and data["edge"]["pooled"]["p_value"] > 0.05
        assert SIGNIFICANT_SENTENCE not in html_doc
        assert "not statistically significant even when measured across the full window" in html_doc

    def test_significant_pooled_window_keeps_the_original_sentence(self, tmp_path):
        path = _two_month_workbook(tmp_path, [20, 25, 30, 35, 40], [20, 25, 30])
        html_doc, data = build_report(_args(path, month="2026-07"))
        assert data["edge"]["pooled"]["p_value"] < 0.05
        assert SIGNIFICANT_SENTENCE in html_doc


class TestCliHelp:
    def test_help_says_a_workbook_is_accepted(self):
        repo_root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, "-m", "spx_trade_desk.tradelog.report.generate_report", "--help"],
            cwd=repo_root, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert "xlsx" in result.stdout.lower()


class TestReviewFixes:
    def test_a_label_with_markup_is_escaped_everywhere_in_the_report(self, ledger_path):
        label = "<img src=x onerror=alert(1)> R&D"
        html_doc, _data = build_report(_args(ledger_path, label=label))
        assert "<img src=x" not in html_doc
        assert "&lt;img src=x onerror=alert(1)&gt; R&amp;D" in html_doc

    def test_skipped_rows_are_stated_in_the_report_and_warned_by_the_tool(self, tmp_path, monkeypatch):
        monkeypatch.setattr("spx_trade_desk.mcp.server.REPORT_OUTPUT_DIR", tmp_path / "out")
        bad = [(46000, "IBKR", "U***12345", "SPXW", "SPXW  260601P05000000", "x", "Sell", 1, 0.5, -1.0, 49.0)]
        path = str(write_ledger(tmp_path / "bad.xlsx", spx_rows=SPX_ROWS + bad))
        html_doc, data = build_report(_args(path))
        assert data["source"]["skipped_rows"] == 1
        assert "Skipped: 1 workbook row" in html_doc
        result = generate_monthly_report(paths=[path], label="Skip Test", offline=True)
        assert any("skipped" in w for w in result["warnings"])

    def test_a_clean_workbook_reports_no_skipped_rows(self, ledger_path):
        html_doc, data = build_report(_args(ledger_path))
        assert data["source"]["skipped_rows"] == 0 and "Skipped:" not in html_doc
