"""Broker file import tests.

Built against synthetic files shaped like a Groww export. The real column
headers are not publicly documented and differ between report types, so the
tests exercise the part that matters: that detection is tolerant, that a bad
guess is visible rather than silent, and that re-importing changes nothing.
"""

from __future__ import annotations

import io
from datetime import date, timedelta

import pandas as pd
import pytest

from src.importers import groww


@pytest.fixture(autouse=True)
def clean_db(tmp_path, monkeypatch):
    import src.db as database
    from sqlalchemy import create_engine

    engine = create_engine(
        f"sqlite:///{tmp_path/'import.db'}", connect_args={"check_same_thread": False}
    )
    monkeypatch.setattr(database, "_engine", engine)
    database.metadata.create_all(engine)

    # Give the resolver a known universe instead of reaching for NSE.
    with database.connection() as conn:
        conn.execute(
            database.stocks.insert(),
            [
                {"symbol": "RELIANCE", "name": "Reliance Industries Ltd"},
                {"symbol": "INFY", "name": "Infosys Ltd"},
                {"symbol": "TITAN", "name": "Titan Company Ltd"},
            ],
        )
    monkeypatch.setattr(groww, "build_symbol_index", lambda: {
        "reliance": "RELIANCE", "reliance industries": "RELIANCE",
        "infy": "INFY", "infosys": "INFY",
        "titan": "TITAN", "titan company": "TITAN",
    })
    yield


def groww_frame(rows=None):
    """A frame shaped like a Groww order-history export."""
    return pd.DataFrame(rows or [
        {"Stock Name": "Reliance Industries Ltd", "Type": "Buy", "Quantity": 10,
         "Price": 1200.50, "Order Date": "15-01-2026", "Order ID": "GRW001", "Exchange": "NSE"},
        {"Stock Name": "Infosys Ltd", "Type": "Buy", "Quantity": 25,
         "Price": 1450.00, "Order Date": "20-02-2026", "Order ID": "GRW002", "Exchange": "NSE"},
        {"Stock Name": "Reliance Industries Ltd", "Type": "Sell", "Quantity": 5,
         "Price": 1380.00, "Order Date": "10-06-2026", "Order ID": "GRW003", "Exchange": "NSE"},
    ])


class TestColumnDetection:
    def test_detects_a_groww_shaped_header(self):
        mapping = groww.detect_mapping(groww_frame())

        assert mapping["symbol"] == "Stock Name"
        assert mapping["side"] == "Type"
        assert mapping["quantity"] == "Quantity"
        assert mapping["price"] == "Price"
        assert mapping["trade_date"] == "Order Date"
        assert mapping["order_id"] == "Order ID"

    def test_handles_alternative_header_names(self):
        frame = pd.DataFrame([{
            "Trading Symbol": "RELIANCE", "Transaction Type": "BUY", "Qty": 10,
            "Avg Price": 1200, "Trade Date": "15-01-2026",
        }])
        mapping = groww.detect_mapping(frame)

        assert mapping["symbol"] == "Trading Symbol"
        assert mapping["side"] == "Transaction Type"
        assert mapping["quantity"] == "Qty"
        assert mapping["price"] == "Avg Price"

    def test_a_column_is_never_claimed_twice(self):
        mapping = groww.detect_mapping(groww_frame())
        chosen = [c for c in mapping.values() if c]
        assert len(chosen) == len(set(chosen))

    def test_an_unmatched_field_is_reported_as_none(self):
        frame = pd.DataFrame([{"Stock Name": "Infosys Ltd", "Qty": 5}])
        mapping = groww.detect_mapping(frame)

        assert mapping["symbol"] == "Stock Name"
        assert mapping["price"] is None
        assert mapping["side"] is None


class TestValueParsing:
    @pytest.mark.parametrize("value,expected", [
        ("Buy", "BUY"), ("BUY", "BUY"), ("bought", "BUY"), ("B", "BUY"),
        ("Sell", "SELL"), ("SOLD", "SELL"), ("S", "SELL"),
        ("", None), ("transfer", None),
    ])
    def test_side_words(self, value, expected):
        assert groww._parse_side(value) == expected

    def test_numbers_survive_currency_formatting(self):
        assert groww._parse_number("Rs 1,200.50") == pytest.approx(1200.50)
        assert groww._parse_number("1,45,000") == pytest.approx(145000)
        assert groww._parse_number("-") is None

    def test_dates_are_read_day_first(self):
        """Indian broker files are DD-MM-YYYY; 03-04-2026 is 3 April."""
        assert groww._parse_date("03-04-2026") == date(2026, 4, 3)
        assert groww._parse_date("15/01/2026") == date(2026, 1, 15)
        assert groww._parse_date("nonsense") is None


class TestSymbolResolution:
    def test_company_name_resolves_to_the_nse_symbol(self):
        index = groww.build_symbol_index()
        assert groww.resolve_symbol("Reliance Industries Ltd", index) == "RELIANCE"
        assert groww.resolve_symbol("Infosys Limited", index) == "INFY"

    def test_a_bare_symbol_resolves(self):
        index = groww.build_symbol_index()
        assert groww.resolve_symbol("INFY", index) == "INFY"
        assert groww.resolve_symbol("RELIANCE.NS", index) == "RELIANCE"

    def test_an_unknown_name_returns_none_rather_than_a_guess(self):
        """A wrong symbol attaches trades to the wrong company silently."""
        index = groww.build_symbol_index()
        assert groww.resolve_symbol("Some Company Nobody Has", index) is None


class TestPreview:
    def test_a_clean_file_is_all_ready(self):
        preview = groww.build_preview(groww_frame())

        assert len(preview.ready) == 3
        assert not preview.problems
        assert not preview.duplicates

    def test_bad_rows_are_flagged_not_dropped(self):
        frame = groww_frame([
            {"Stock Name": "Reliance Industries Ltd", "Type": "Buy", "Quantity": 10,
             "Price": 1200, "Order Date": "15-01-2026", "Order ID": "A1", "Exchange": "NSE"},
            {"Stock Name": "Unknown Corp", "Type": "Buy", "Quantity": 5,
             "Price": 100, "Order Date": "15-01-2026", "Order ID": "A2", "Exchange": "NSE"},
            {"Stock Name": "Infosys Ltd", "Type": "Buy", "Quantity": 0,
             "Price": 1450, "Order Date": "15-01-2026", "Order ID": "A3", "Exchange": "NSE"},
        ])
        preview = groww.build_preview(frame)

        assert len(preview.rows) == 3, "every row is accounted for"
        assert len(preview.ready) == 1
        assert len(preview.problems) == 2
        assert "Unknown Corp" in preview.unresolved_names

    def test_a_misnamed_column_shows_up_as_problems(self):
        """If detection picks wrong, the preview makes it obvious."""
        frame = groww_frame()
        broken = dict(groww.detect_mapping(frame))
        broken["quantity"] = "Price"      # deliberately wrong
        broken["price"] = "Quantity"

        preview = groww.build_preview(frame, mapping=broken)
        quantities = [r.quantity for r in preview.rows]
        assert quantities[0] == pytest.approx(1200.50), "the mapping was honoured, wrongly"
        assert preview.mapping["quantity"] == "Price"

    def test_duplicates_within_one_file_are_caught(self):
        row = {"Stock Name": "Infosys Ltd", "Type": "Buy", "Quantity": 10,
               "Price": 1450, "Order Date": "15-01-2026", "Order ID": "SAME", "Exchange": "NSE"}
        preview = groww.build_preview(pd.DataFrame([row, dict(row)]))

        assert len(preview.ready) == 1
        assert len(preview.duplicates) == 1

    def test_a_future_dated_row_is_rejected(self):
        tomorrow = (date.today() + timedelta(days=1)).strftime("%d-%m-%Y")
        frame = groww_frame([{
            "Stock Name": "Infosys Ltd", "Type": "Buy", "Quantity": 10,
            "Price": 1450, "Order Date": tomorrow, "Order ID": "F1", "Exchange": "NSE",
        }])
        preview = groww.build_preview(frame)
        assert any("future" in p for p in preview.rows[0].problems)


class TestCommit:
    def test_imports_and_builds_positions(self):
        from src import portfolio as pf

        result = groww.commit(groww.build_preview(groww_frame()))
        assert result["imported"] == 3
        assert not result["failures"]

        position_id = pf.find_open_position("RELIANCE")
        state = pf.state_for(position_id)

        assert state.quantity == 5, "10 bought, 5 sold"
        assert state.realised_gain > 0

    def test_reimporting_the_same_file_adds_nothing(self):
        """The property that makes overlapping exports safe."""
        first = groww.commit(groww.build_preview(groww_frame()))
        assert first["imported"] == 3

        second_preview = groww.build_preview(groww_frame())
        assert len(second_preview.duplicates) == 3
        assert not second_preview.ready

        second = groww.commit(second_preview)
        assert second["imported"] == 0

    def test_rows_are_applied_oldest_first(self):
        """FIFO needs the purchase recorded before the sale that consumes it."""
        reversed_frame = groww_frame().iloc[::-1].reset_index(drop=True)
        result = groww.commit(groww.build_preview(reversed_frame))

        assert result["imported"] == 3
        assert not result["failures"], "a sale before its purchase would fail"

    def test_charges_from_the_file_are_used_when_present(self):
        from src import portfolio as pf

        frame = pd.DataFrame([{
            "Stock Name": "Infosys Ltd", "Type": "Buy", "Quantity": 10,
            "Price": 1000, "Order Date": "15-01-2026", "Order ID": "C1",
            "Exchange": "NSE", "Total Charges": 33.50,
        }])
        groww.commit(groww.build_preview(frame))

        transactions = pf.all_transactions("INFY")
        assert transactions[0]["total_charges"] == pytest.approx(33.50)

    def test_charges_are_estimated_when_absent(self):
        from src import portfolio as pf

        groww.commit(groww.build_preview(groww_frame()))
        transactions = pf.all_transactions("RELIANCE")
        assert all(t["total_charges"] > 0 for t in transactions)


class TestFileReading:
    def test_reads_csv_bytes(self):
        buffer = io.StringIO()
        groww_frame().to_csv(buffer, index=False)
        frame = groww.read_file(buffer.getvalue().encode(), "orders.csv")
        assert len(frame) == 3

    def test_reads_xlsx_bytes(self):
        buffer = io.BytesIO()
        groww_frame().to_excel(buffer, index=False)
        frame = groww.read_file(buffer.getvalue(), "orders.xlsx")
        assert len(frame) == 3

    def test_skips_a_title_preamble(self):
        """Broker exports often open with a title and account number."""
        buffer = io.StringIO()
        buffer.write("Groww Order History\n")
        buffer.write("Account: XX1234\n")
        buffer.write("\n")
        groww_frame().to_csv(buffer, index=False)

        frame = groww.read_file(buffer.getvalue().encode(), "orders.csv")
        assert "Stock Name" in frame.columns
        assert len(frame) == 3
