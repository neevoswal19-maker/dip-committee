"""Adding columns to a database that is already deployed.

`create_all` never alters an existing table, so the `strategy` columns would
simply be missing on Neon and every query naming them would fail. The
migration adds them in place; it has to work on a database built before
they existed, and do nothing the second time.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text

from src import db


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path/'legacy.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE positions (id INTEGER PRIMARY KEY, symbol VARCHAR(32), status VARCHAR(16))"))
        conn.execute(text("CREATE TABLE committee_runs (id INTEGER PRIMARY KEY, symbol VARCHAR(32))"))
        conn.execute(text("INSERT INTO positions (symbol, status) VALUES ('INDIANB', 'open')"))
    monkeypatch.setattr(db, "_engine", engine)
    return engine


def _columns(engine, table):
    return {c["name"] for c in inspect(engine).get_columns(table)}


def test_adds_the_missing_columns(legacy):
    added = db._migrate(legacy)
    assert set(added) == {"positions.strategy", "committee_runs.strategy"}
    assert "strategy" in _columns(legacy, "positions")
    assert "strategy" in _columns(legacy, "committee_runs")


def test_existing_rows_survive_and_read_as_null(legacy):
    db._migrate(legacy)
    with legacy.begin() as conn:
        row = conn.execute(text("SELECT symbol, strategy FROM positions")).first()
    assert row.symbol == "INDIANB"
    assert row.strategy is None           # counts as long-term in the ledger


def test_running_it_again_changes_nothing(legacy):
    db._migrate(legacy)
    assert db._migrate(legacy) == []


def test_a_fresh_database_needs_no_migration(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path/'fresh.db'}")
    monkeypatch.setattr(db, "_engine", engine)
    db.init_db()
    assert "strategy" in _columns(engine, "positions")
    assert "swing_signals" in inspect(engine).get_table_names()
    assert db._migrate(engine) == []


def test_null_strategy_counts_as_long_term(tmp_path, monkeypatch):
    from datetime import date

    from src import portfolio as pf

    engine = create_engine(f"sqlite:///{tmp_path/'ledger.db'}")
    monkeypatch.setattr(db, "_engine", engine)
    db.init_db()
    position_id = pf.create_position("INDIANB")
    with engine.begin() as conn:
        conn.execute(text("UPDATE positions SET strategy = NULL"))   # as an old row would be
    pf.record_trade("INDIANB", "BUY", date(2026, 9, 1), 10, 800.0, position_id=position_id)

    assert pf.find_open_position("INDIANB", pf.LONG_TERM) == position_id
    assert pf.find_open_position("INDIANB", pf.SWING) is None
    assert [s.symbol for s in pf.open_positions(strategy=pf.LONG_TERM)] == ["INDIANB"]
    assert pf.open_positions(strategy=pf.SWING) == []
