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


# --- Option (a), 2026-10-26: alert only the first time a stock becomes a BUY ----------


def _alerted(symbol: str, days_ago: float, delivered: bool = True):
    from datetime import timedelta

    with db.connection() as conn:
        conn.execute(db.alerts_sent.insert().values(
            sent_at=db.now() - timedelta(days=days_ago), channel="telegram", alert_type="new_buy_candidate",
            symbol=symbol, dedupe_key=f"buy|{symbol}|{days_ago}", body="", delivered=delivered))


def _verdict(symbol: str, stance: str, days_ago: float):
    from datetime import timedelta

    with db.connection() as conn:
        conn.execute(db.committee_runs.insert().values(
            symbol=symbol, run_at=db.now() - timedelta(days=days_ago), stance=stance, conviction=55.0))


def test_a_stock_never_alerted_alerts(fresh_db):
    from jobs.daily_scan import first_buy_of_episode

    assert first_buy_of_episode("FRESH", CFG) is True


def test_a_stock_that_stays_a_buy_does_not_alert_again(fresh_db):
    from jobs.daily_scan import first_buy_of_episode

    _alerted("STILLBUY", days_ago=3)
    _verdict("STILLBUY", "BUY", days_ago=2)
    _verdict("STILLBUY", "BUY", days_ago=1)
    assert first_buy_of_episode("STILLBUY", CFG) is False


def test_a_buy_that_lapsed_and_returned_alerts_again(fresh_db):
    from jobs.daily_scan import first_buy_of_episode

    _alerted("CAMEBACK", days_ago=6)
    _verdict("CAMEBACK", "WATCH", days_ago=3)
    _verdict("CAMEBACK", "BUY", days_ago=0.1)
    assert first_buy_of_episode("CAMEBACK", CFG) is True


def test_an_alert_older_than_the_window_alerts_again(fresh_db):
    from jobs.daily_scan import first_buy_of_episode

    _alerted("LONGAGO", days_ago=CFG.get("alerts.buy_realert_days") + 2)
    assert first_buy_of_episode("LONGAGO", CFG) is True


def test_a_send_that_never_arrived_does_not_count(fresh_db):
    from jobs.daily_scan import first_buy_of_episode

    _alerted("FAILEDSEND", days_ago=1, delivered=False)
    assert first_buy_of_episode("FAILEDSEND", CFG) is True


def test_the_alert_loop_skips_a_buy_already_alerted(fresh_db, monkeypatch):
    from jobs import daily_scan

    _seed(date(2026, 10, 26))
    _alerted("NEWBUY", days_ago=1)                 # alerted yesterday, still a BUY today
    sent = []
    monkeypatch.setattr(daily_scan.committee_module, "rebuild_report",
                        lambda run: SimpleNamespace(symbol=run["symbol"]))
    monkeypatch.setattr(daily_scan.telegram, "buy_candidate", lambda report, cfg: f"BUY {report.symbol}")
    monkeypatch.setattr(daily_scan.telegram, "send", lambda body, **kw: (sent.append(body), True)[1])
    assert daily_scan.alert_on_buys(CFG) == [] and sent == []
