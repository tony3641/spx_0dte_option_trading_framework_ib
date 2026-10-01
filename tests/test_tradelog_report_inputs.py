"""Report inputs: source dispatch, the QFX loader and the workbook loader."""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from spx_trade_desk.tradelog.report.report_inputs import (
    DEFAULT_LEDGER_CAPITAL,
    ReportInputs,
    load_ledger_inputs,
    load_qfx_inputs,
    load_report_inputs,
    report_source_kind,
)
from tests.ledger_fixture import DAILY_PNL, write_ledger
from tests.test_tradelog_analysis import SYNTHETIC_QFX


@pytest.fixture
def qfx_path(tmp_path):
    path = tmp_path / "synthetic.qfx"
    path.write_text(SYNTHETIC_QFX, encoding="latin-1")
    return str(path)


class TestSourceKind:
    @pytest.mark.parametrize("name,kind", [
        ("a.qfx", "qfx"), ("A.QFX", "qfx"),
        ("a.xlsx", "ledger"), ("A.XLSX", "ledger"), ("a.xlsm", "ledger"),
    ])
    def test_dispatches_on_extension(self, name, kind):
        assert report_source_kind(f"/some/dir/{name}") == kind

    def test_unsupported_type_names_the_file_and_the_accepted_types(self):
        with pytest.raises(ValueError) as exc:
            report_source_kind("/some/dir/statement.csv")
        message = str(exc.value)
        assert "statement.csv" in message and "QFX" in message and ".xlsx" in message

    def test_display_name_replaces_the_temp_name_in_the_message(self):
        with pytest.raises(ValueError, match="my_upload.pdf"):
            report_source_kind("/tmp/upload_0.pdf", display_name="my_upload.pdf")

    def test_file_without_extension_is_rejected_clearly(self):
        with pytest.raises(ValueError, match="no extension"):
            report_source_kind("/some/dir/statement")


class TestQfxInputs:
    def test_carries_positions_and_a_single_account_label(self, qfx_path):
        inputs = load_qfx_inputs(SimpleNamespace(monthly=qfx_path, ytd=None))
        assert isinstance(inputs, ReportInputs)
        assert inputs.has_positions
        assert inputs.account_label == "Account U123456"
        assert inputs.source is None
        assert inputs.prior_daily is None and inputs.prior_from_window is False
        assert not inputs.daily.empty and not inputs.positions.empty

    def test_ytd_statement_becomes_the_prior_months(self, qfx_path):
        inputs = load_qfx_inputs(SimpleNamespace(monthly=qfx_path, ytd=qfx_path))
        assert inputs.prior_daily is not None and not inputs.prior_daily.empty
        assert inputs.prior_capital is not None


@pytest.fixture
def ledger_path(tmp_path):
    return str(write_ledger(tmp_path / "trade_log.xlsx"))


def _args(path, **over):
    base = dict(monthly=path, ytd=None)
    base.update(over)
    return SimpleNamespace(**base)


class TestLedgerInputs:
    def test_daily_pnl_is_the_spx_spxw_option_rows_only(self, ledger_path):
        inputs = load_ledger_inputs(_args(ledger_path))
        assert list(inputs.daily["realized_pnl"]) == pytest.approx(DAILY_PNL)
        assert list(inputs.daily["activity_date"])[0] == date(2026, 6, 1)
        assert len(inputs.daily) == 6  # stock, dividend, fee and ABCD rows add no days

    def test_has_no_positions_and_reads_prior_months_from_its_own_window(self, ledger_path):
        inputs = load_ledger_inputs(_args(ledger_path))
        assert inputs.has_positions is False
        assert inputs.positions is None and inputs.spreads is None
        assert inputs.prior_from_window is True and inputs.prior_daily is None

    def test_provenance_lists_accounts_scope_and_coverage(self, ledger_path):
        source = load_ledger_inputs(_args(ledger_path)).source
        assert source["kind"] == "ledger"
        assert source["accounts"] == ["E*Trade", "U***12345"]
        assert source["scope"] == "SPX/SPXW options"
        assert source["contract_days"] == 14
        assert source["closed_intraday"] == 1  # the 07-06 short bought back the same day

    def test_capital_defaults_to_an_assumed_amount(self, ledger_path):
        inputs = load_ledger_inputs(_args(ledger_path))
        assert inputs.initial_capital == DEFAULT_LEDGER_CAPITAL == 100_000.0
        assert inputs.source["capital_assumed"] is True
        assert inputs.source["capital_at_start"] == 100_000.0

    def test_supplied_capital_is_not_marked_assumed(self, ledger_path):
        inputs = load_ledger_inputs(_args(ledger_path, initial_capital=50_000))
        assert inputs.initial_capital == 50_000.0
        assert inputs.source["capital_assumed"] is False

    @pytest.mark.parametrize("bad", [0, -5, 0.0])
    def test_non_positive_capital_is_rejected(self, ledger_path, bad):
        with pytest.raises(ValueError, match="initial_capital"):
            load_ledger_inputs(_args(ledger_path, initial_capital=bad))

    def test_account_filter_narrows_to_one_account(self, ledger_path):
        inputs = load_ledger_inputs(_args(ledger_path, account="E*Trade"))
        assert list(inputs.daily["realized_pnl"]) == pytest.approx([57.0])
        assert inputs.source["accounts"] == ["E*Trade"]
        assert inputs.account_label == "Account E*Trade"

    def test_unknown_account_lists_the_available_ones(self, ledger_path):
        with pytest.raises(ValueError) as exc:
            load_ledger_inputs(_args(ledger_path, account="NOPE"))
        assert "NOPE" in str(exc.value) and "U***12345" in str(exc.value)

    def test_all_accounts_label_names_every_account(self, ledger_path):
        assert load_ledger_inputs(_args(ledger_path)).account_label == "Accounts E*Trade, U***12345"

    def test_workbook_without_spx_rows_gives_an_empty_daily_series_not_a_crash(self, tmp_path):
        path = write_ledger(tmp_path / "no_spx.xlsx", spx_rows=[],
                            other_tabs=True)
        inputs = load_ledger_inputs(_args(str(path)))
        assert inputs.daily.empty
        assert {"activity_date", "realized_pnl"} <= set(inputs.daily.columns)
        assert inputs.source["contract_days"] == 0 and inputs.source["accounts"] == []


class TestReportInputsDispatch:
    def test_workbook_is_routed_to_the_ledger_loader(self, ledger_path):
        assert load_report_inputs(_args(ledger_path)).source["kind"] == "ledger"

    def test_uppercase_extension_is_routed_too(self, tmp_path):
        path = write_ledger(tmp_path / "TRADE_LOG.XLSX")
        assert load_report_inputs(_args(str(path))).source["kind"] == "ledger"

    def test_ytd_is_rejected_with_a_workbook(self, ledger_path):
        with pytest.raises(ValueError, match="ytd"):
            load_report_inputs(_args(ledger_path, ytd="earlier.qfx"))

    def test_unsupported_type_is_rejected_before_any_parsing(self, tmp_path):
        path = tmp_path / "statement.csv"
        path.write_text("not,a,qfx")
        with pytest.raises(ValueError, match="statement.csv"):
            load_report_inputs(_args(str(path)))
