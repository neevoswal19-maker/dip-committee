"""Ledger and FIFO matching tests.

FIFO is not a modelling choice here - Indian tax rules match sales against the
earliest purchases, so getting it wrong makes the short-term/long-term split
confidently false. Expected values below are computed by hand in the
docstrings rather than copied from a run.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from src import portfolio as pf
from src.portfolio import LedgerError


def buy(id_, day, qty, price, charges=0.0):
    """A buy row as the matcher expects it. Net = turnover + charges."""
    return {
        "id": id_, "trade_date": day, "quantity": qty, "price": price,
        "total_charges": charges, "net_amount": qty * price + charges,
        "side": "BUY", "symbol": "TEST",
    }


def sell(id_, day, qty, price, charges=0.0):
    """A sell row. Net = turnover - charges."""
    return {
        "id": id_, "trade_date": day, "quantity": qty, "price": price,
        "total_charges": charges, "net_amount": qty * price - charges,
        "side": "SELL", "symbol": "TEST",
    }


JAN = date(2026, 1, 10)
MAR = date(2026, 3, 10)
JUN = date(2026, 6, 10)


class TestFifoMatching:
    def test_partial_sell_consumes_the_oldest_lot_first(self):
        """10 @ 100 (Jan) + 10 @ 120 (Mar), sell 5 in June.

        FIFO takes the 5 from January, so 5 @ 100 and 10 @ 120 remain.
        """
        lots, disposals = pf.match_fifo(
            [buy(1, JAN, 10, 100), buy(2, MAR, 10, 120)],
            [sell(3, JUN, 5, 150)],
        )

        assert len(lots) == 2
        assert lots[0].remaining == 5      # January lot, half eaten
        assert lots[0].price == 100
        assert lots[1].remaining == 10     # March lot, untouched

        assert len(disposals) == 1
        assert disposals[0].quantity == 5
        assert disposals[0].buy_date == JAN
        assert disposals[0].gain == pytest.approx(250.0)   # 5 * (150 - 100)

    def test_sell_that_exactly_empties_a_lot(self):
        lots, disposals = pf.match_fifo(
            [buy(1, JAN, 10, 100), buy(2, MAR, 10, 120)],
            [sell(3, JUN, 10, 150)],
        )

        assert len(lots) == 1, "an emptied lot must not linger"
        assert lots[0].price == 120
        assert lots[0].remaining == 10
        assert len(disposals) == 1
        assert disposals[0].gain == pytest.approx(500.0)

    def test_sell_spanning_three_lots(self):
        """10@100 + 10@120 + 10@140, sell 25 at 150.

        Gains: 10*(150-100) + 10*(150-120) + 5*(150-140)
             = 500 + 300 + 50 = 850
        """
        lots, disposals = pf.match_fifo(
            [buy(1, JAN, 10, 100), buy(2, MAR, 10, 120), buy(3, JUN, 10, 140)],
            [sell(4, date(2026, 9, 10), 25, 150)],
        )

        assert len(disposals) == 3
        assert [d.quantity for d in disposals] == [10, 10, 5]
        assert sum(d.gain for d in disposals) == pytest.approx(850.0)

        assert len(lots) == 1
        assert lots[0].remaining == 5
        assert lots[0].price == 140

    def test_oversized_sell_is_rejected(self):
        with pytest.raises(LedgerError, match="exceeds shares held"):
            pf.match_fifo([buy(1, JAN, 10, 100)], [sell(2, JUN, 15, 150)])

    def test_oversized_sell_is_tolerated_when_not_strict(self):
        """A broker export can start mid-history, selling shares bought earlier."""
        lots, disposals = pf.match_fifo(
            [buy(1, JAN, 10, 100)], [sell(2, JUN, 15, 150)], strict=False
        )
        assert lots == []
        assert sum(d.quantity for d in disposals) == 10

    def test_full_exit_leaves_nothing(self):
        lots, disposals = pf.match_fifo(
            [buy(1, JAN, 10, 100), buy(2, MAR, 5, 120)],
            [sell(3, JUN, 15, 150)],
        )
        assert lots == []
        assert sum(d.quantity for d in disposals) == 15

    def test_charges_are_folded_into_the_cost_basis(self):
        """10 @ 100 plus Rs 24 of charges: (1000 + 24) / 10 = 102.40 a share."""
        lots, _ = pf.match_fifo([buy(1, JAN, 10, 100, charges=24.0)], [])
        assert lots[0].cost_per_share == pytest.approx(102.40)
        assert lots[0].price == 100, "the traded price stays visible separately"

    def test_sale_proceeds_are_net_of_charges(self):
        """Sell 10 @ 150 with Rs 46 of charges nets 145.4 per share."""
        _, disposals = pf.match_fifo(
            [buy(1, JAN, 10, 100)], [sell(2, JUN, 10, 150, charges=46.0)]
        )
        assert disposals[0].sell_price_per_share == pytest.approx(145.4)
        assert disposals[0].gain == pytest.approx(454.0)   # 10 * (145.4 - 100)


class TestHoldingPeriod:
    def test_long_term_boundary(self):
        """Exactly 366 days is long-term; 365 is not."""
        entry = date(2025, 1, 1)
        _, short = pf.match_fifo(
            [buy(1, entry, 10, 100)], [sell(2, entry + timedelta(days=365), 10, 150)]
        )
        _, long = pf.match_fifo(
            [buy(1, entry, 10, 100)], [sell(2, entry + timedelta(days=366), 10, 150)]
        )

        assert short[0].is_long_term is False
        assert long[0].is_long_term is True

    def test_each_lot_keeps_its_own_clock(self):
        """Three tranches 60 days apart reach long-term on three dates.

        This is the whole reason lots exist rather than an average.
        """
        lots, _ = pf.match_fifo(
            [
                buy(1, date(2026, 1, 1), 10, 100),
                buy(2, date(2026, 3, 1), 10, 95),
                buy(3, date(2026, 5, 1), 10, 105),
            ],
            [],
        )

        ltcg_dates = [lot.ltcg_date(366) for lot in lots]
        assert len(set(ltcg_dates)) == 3
        assert ltcg_dates[0] == date(2027, 1, 2)
        assert ltcg_dates[0] < ltcg_dates[1] < ltcg_dates[2]

    def test_a_sale_can_be_part_short_and_part_long_term(self):
        """FIFO can straddle the boundary within one sale."""
        lots, disposals = pf.match_fifo(
            [
                buy(1, date(2025, 1, 1), 10, 100),   # long-term by the sale date
                buy(2, date(2026, 6, 1), 10, 120),   # short-term
            ],
            [sell(3, date(2026, 9, 1), 15, 150)],
        )

        assert len(disposals) == 2
        assert disposals[0].is_long_term is True
        assert disposals[1].is_long_term is False
        assert disposals[0].quantity == 10
        assert disposals[1].quantity == 5


class TestLedgerIntegration:
    """Against a real database, so derivation and persistence are both tested."""

    @pytest.fixture(autouse=True)
    def clean_db(self, tmp_path, monkeypatch):
        import src.db as database

        monkeypatch.setattr(database, "_engine", None)
        monkeypatch.setattr(
            database, "database_url", lambda: f"sqlite:///{tmp_path/'test.db'}", raising=False
        )
        from sqlalchemy import create_engine

        engine = create_engine(
            f"sqlite:///{tmp_path/'test.db'}", connect_args={"check_same_thread": False}
        )
        monkeypatch.setattr(database, "_engine", engine)
        database.metadata.create_all(engine)
        yield

    def test_average_cost_is_derived_not_stored(self):
        """Buy 10 @ 100 then 10 @ 120 -> average 110, ignoring charges."""
        pf.record_trade("TEST", "BUY", JAN, 10, 100, charges={"total": 0.0})
        position_id, state = pf.record_trade("TEST", "BUY", MAR, 10, 120, charges={"total": 0.0})

        assert state.quantity == 20
        assert state.avg_cost == pytest.approx(110.0)

        # Re-derive from a fresh read: proves nothing was cached in the row.
        assert pf.state_for(position_id).avg_cost == pytest.approx(110.0)

    def test_the_positions_row_is_kept_in_step(self):
        from sqlalchemy import select
        from src import db as database

        position_id, _ = pf.record_trade("TEST", "BUY", JAN, 10, 100, charges={"total": 0.0})
        pf.record_trade("TEST", "BUY", MAR, 10, 120, charges={"total": 0.0})

        with database.connection() as conn:
            row = conn.execute(
                select(database.positions).where(database.positions.c.id == position_id)
            ).first()

        assert row.quantity == pytest.approx(20)
        assert row.avg_entry_price == pytest.approx(110.0)
        assert row.status == "open"
        assert row.entry_date == JAN, "entry date is the first purchase"

    def test_tranches_then_a_trim(self):
        """The strategy's actual shape: three buys, then sell a quarter."""
        pf.record_trade("TEST", "BUY", JAN, 20, 100, charges={"total": 0.0}, tranche_label="T1")
        pf.record_trade("TEST", "BUY", MAR, 10, 93, charges={"total": 0.0}, tranche_label="T2")
        pf.record_trade("TEST", "BUY", JUN, 10, 110, charges={"total": 0.0}, tranche_label="T3")

        position_id, state = pf.record_trade(
            "TEST", "SELL", date(2026, 9, 1), 10, 150, charges={"total": 0.0}, tranche_label="trim"
        )

        assert state.quantity == 30
        assert state.status == "open"
        # The trim took 10 from the January lot at 100.
        assert state.realised_gain == pytest.approx(500.0)
        assert len(state.lots) == 3
        assert state.lots[0].remaining == 10

    def test_selling_more_than_held_is_refused(self):
        pf.record_trade("TEST", "BUY", JAN, 10, 100, charges={"total": 0.0})
        with pytest.raises(LedgerError, match="only 10 held"):
            pf.record_trade("TEST", "SELL", JUN, 15, 150, charges={"total": 0.0})

    def test_selling_with_no_position_is_refused(self):
        with pytest.raises(LedgerError, match="no open position"):
            pf.record_trade("NOTHELD", "SELL", JUN, 5, 150, charges={"total": 0.0})

    def test_full_exit_closes_the_position_and_writes_a_trade(self):
        from sqlalchemy import select
        from src import db as database

        pf.record_trade("TEST", "BUY", JAN, 10, 100, charges={"total": 0.0})
        position_id, state = pf.record_trade(
            "TEST", "SELL", JUN, 10, 150, charges={"total": 0.0}
        )

        assert state.status == "closed"
        assert state.quantity == 0

        with database.connection() as conn:
            trade = conn.execute(
                select(database.trades).where(database.trades.c.position_id == position_id)
            ).first()

        assert trade is not None, "a closed position must reach the learning ledger"
        assert trade.return_pct == pytest.approx(50.0)
        assert trade.is_win is True

    def test_a_second_buy_reopens_nothing_unexpected(self):
        """After a full exit, a new buy starts a fresh position."""
        pf.record_trade("TEST", "BUY", JAN, 10, 100, charges={"total": 0.0})
        first_id, _ = pf.record_trade("TEST", "SELL", MAR, 10, 150, charges={"total": 0.0})
        second_id, state = pf.record_trade("TEST", "BUY", JUN, 5, 120, charges={"total": 0.0})

        assert second_id != first_id
        assert state.quantity == 5

    def test_deleting_a_transaction_resyncs(self):
        position_id, _ = pf.record_trade("TEST", "BUY", JAN, 10, 100, charges={"total": 0.0})
        transactions = pf.transactions_for(position_id)

        _, state = pf.record_trade("TEST", "BUY", MAR, 10, 200, charges={"total": 0.0})
        assert state.avg_cost == pytest.approx(150.0)

        wrong = pf.transactions_for(position_id)[-1]
        pf.delete_transaction(wrong["id"])

        assert pf.state_for(position_id).avg_cost == pytest.approx(100.0)

    def test_duplicate_order_id_is_rejected(self):
        """The guard that makes re-importing a broker file a no-op."""
        import sqlalchemy.exc

        pf.record_trade(
            "TEST", "BUY", JAN, 10, 100, charges={"total": 0.0},
            broker="groww", order_id="ORDER123",
        )
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            pf.record_trade(
                "TEST", "BUY", JAN, 10, 100, charges={"total": 0.0},
                broker="groww", order_id="ORDER123",
            )

    def test_synthetic_order_id_is_stable(self):
        first = pf.synthetic_order_id("TEST", "BUY", JAN, 10, 100)
        second = pf.synthetic_order_id("TEST", "BUY", JAN, 10, 100)
        different = pf.synthetic_order_id("TEST", "BUY", JAN, 11, 100)

        assert first == second, "the same trade must hash identically"
        assert first != different

    def test_charges_default_to_the_estimator(self):
        """Omitting charges must not silently record a free trade."""
        _, state = pf.record_trade("TEST", "BUY", JAN, 10, 1000)
        assert state.total_charges > 0
        assert state.avg_cost > 1000

    def test_backfill_gives_legacy_positions_a_transaction(self):
        from src import db as database

        with database.connection() as conn:
            position_id = conn.execute(
                database.positions.insert().values(
                    symbol="OLD", status="open", entry_date=JAN,
                    avg_entry_price=250.0, quantity=40.0,
                )
            ).inserted_primary_key[0]

        assert pf.backfill_transactions() == 1

        state = pf.state_for(position_id)
        assert state.quantity == 40
        assert state.avg_cost == pytest.approx(250.0)
        assert pf.backfill_transactions() == 0, "backfill must be idempotent"
