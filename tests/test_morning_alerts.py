"""The morning alert step: fast with many candidates, and never stale.

On 2026-10-26 the step took 19 minutes (init_db re-checked every table once
per candidate, 45 candidates) and re-sent Friday's BUY verdicts for two
stocks as if they were new.
"""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from src import db
from src.config import load_config

CFG = load_config()


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path/'alerts.db'}")
    monkeypatch.setattr(db, "_engine", engine)
    db.init_db()
    return engine


def test_init_db_checks_the_tables_once_per_engine(tmp_path, monkeypatch):
    calls = []
    real = db.metadata.create_all
    monkeypatch.setattr(db.metadata, "create_all", lambda engine, **kw: (calls.append(engine), real(engine, **kw)))

    first = create_engine(f"sqlite:///{tmp_path/'a.db'}")
    monkeypatch.setattr(db, "_engine", first)
    for _ in range(5):
        db.init_db()
    assert len(calls) == 1                         # checked once, not five times

    second = create_engine(f"sqlite:///{tmp_path/'b.db'}")
    monkeypatch.setattr(db, "_engine", second)
    db.init_db()
    assert len(calls) == 2                         # a new database is always checked

    db.init_db(drop=True)
    assert len(calls) == 3                         # drop still rebuilds


def _seed(today: date):
    with db.connection() as conn:
        scan_id = conn.execute(db.scans.insert().values(
            run_at=datetime(2026, 10, 26, 7, 50), trade_date=today, universe_size=501, status="complete",
        )).inserted_primary_key[0]
        for rank, symbol in enumerate(("OLDBUY", "NEWBUY", "NEWWATCH"), start=1):
            conn.execute(db.candidates.insert().values(scan_id=scan_id, symbol=symbol, rank=rank, screen_score=50.0))
        conn.execute(db.committee_runs.insert().values(symbol="OLDBUY", trade_date=date(2026, 10, 23),
                                                       stance="BUY", conviction=57.0, price_at_run=100.0))
        conn.execute(db.committee_runs.insert().values(symbol="NEWBUY", trade_date=today,
                                                       stance="BUY", conviction=55.0, price_at_run=200.0))
        conn.execute(db.committee_runs.insert().values(symbol="NEWWATCH", trade_date=today,
                                                       stance="WATCH", conviction=52.0, price_at_run=300.0))


def test_only_todays_buy_verdicts_are_alerted(fresh_db, monkeypatch):
    from jobs import daily_scan

    _seed(date(2026, 10, 23 + 3))
    sent = []
    monkeypatch.setattr(daily_scan.committee_module, "rebuild_report",
                        lambda run: SimpleNamespace(symbol=run["symbol"]))
    monkeypatch.setattr(daily_scan.telegram, "buy_candidate", lambda report, cfg: f"BUY {report.symbol}")
    monkeypatch.setattr(daily_scan.telegram, "send", lambda body, **kw: (sent.append(body), True)[1])

    assert daily_scan.alert_on_buys(CFG) == ["NEWBUY"]
    assert sent == ["BUY NEWBUY"]                  # Friday's OLDBUY verdict is not re-sent
