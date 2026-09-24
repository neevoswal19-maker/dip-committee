"""The morning news check on holdings, and running the morning scan once.

The news check reads the outside world and alerts on it, so the tests pin
what counts as serious (and what routine paperwork must never trigger), that
each item alerts once, and that "could not check" never reads as "no news".
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from src import db
from src import portfolio as pf
from src.alerts import holdings_news
from src.data.provider import DataResult, DataStatus


@pytest.mark.parametrize("text, expected", [
    ("Resignation of Statutory Auditor of the Company", "governance"),
    ("SEBI passes order against promoters for fraud", "governance"),
    ("Show cause notice received from SEBI", "governance"),
    ("Disclosure of encumbrance: promoter pledges 12% stake", "pledge"),
    ("NCLT admits insolvency petition against company", "litigation"),
    ("Fire at Dahej plant halts production", "operational"),
    ("CRISIL downgrades long-term rating to AA-", "rating_downgrade"),
    # Must not alert:
    ("ICRA upgrades rating to AA+", None),
    ("Compliance certificate under Regulation 74(5) of SEBI (DP) Regulations", None),
    ("Intimation of board meeting to consider financial results", None),
    ("Company bags Rs 500 crore order from NHAI", None),
    ("Declares interim dividend of Rs 5 per share", None),
])
def test_what_counts_as_serious(text, expected):
    assert holdings_news.serious_event(text) == expected


def test_the_same_item_has_the_same_key_however_it_is_spaced():
    a = holdings_news.NewsFlag("INDIANB", "governance", "Auditor  resigns", "NSE filing", None)
    b = holdings_news.NewsFlag("INDIANB", "governance", "auditor resigns ", "livemint.com", None)
    c = holdings_news.NewsFlag("SBIN", "governance", "Auditor resigns", "NSE filing", None)
    assert a.dedupe_key == b.dedupe_key
    assert a.dedupe_key != c.dedupe_key


def test_the_message_asks_for_the_item_to_be_read():
    flag = holdings_news.NewsFlag("M&M", "governance", "Auditor <resigns>", "NSE filing",
                                  datetime(2026, 9, 23, 18, 40), held=30)
    body = holdings_news.message(flag)
    assert "M&amp;M" in body and "&lt;resigns&gt;" in body
    assert "you hold 30" in body
    assert "keyword rules" in body


# --- Against a database -----------------------------------------------------------


@pytest.fixture
def held(tmp_path, monkeypatch):
    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{tmp_path/'news.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "_engine", engine)
    db.metadata.create_all(engine)
    monkeypatch.setattr(db, "now", lambda: datetime(2026, 9, 24, 7, 30))
    with db.connection() as conn:
        conn.execute(db.stocks.insert().values(symbol="INDIANB", name="Indian Bank"))
    pf.record_trade("INDIANB", "BUY", date(2026, 9, 1), 30, 800.0)
    return monkeypatch


def _sources(monkeypatch, filings=None, headlines=None, fail=()):
    from src.data import news, nse

    def announcements(symbol, days=90):
        if "filings" in fail:
            raise ConnectionError("NSE down")
        return DataResult(value=pd.DataFrame(filings or []), status=DataStatus.OK, source="test")

    def get_news(identity, days=30, **kwargs):
        if "headlines" in fail:
            raise ConnectionError("RSS down")
        return DataResult(value=headlines or [], status=DataStatus.OK, source="test")

    monkeypatch.setattr(nse, "get_corporate_announcements", announcements)
    monkeypatch.setattr(news, "get_news", get_news)


def test_finds_serious_items_and_ignores_the_rest(held):
    _sources(held,
             filings=[{"desc": "Resignation of Statutory Auditor", "an_dt": "23-Sep-2026 18:40:00"},
                      {"desc": "Compliance certificate under Regulation 74(5)", "an_dt": "23-Sep-2026 10:00:00"}],
             headlines=[{"title": "Indian Bank shares rise 2%", "published": datetime(2026, 9, 23, 12, 0)},
                        {"title": "CRISIL downgrades Indian Bank", "source": "livemint.com",
                         "published": datetime(2026, 9, 23, 15, 0), "link": "https://example.com/a"}])
    flags, unchecked = holdings_news.find()
    assert unchecked == []
    assert sorted(f.event for f in flags) == ["governance", "rating_downgrade"]
    assert all(f.held == 30 for f in flags)


def test_old_items_are_left_to_earlier_mornings(held):
    _sources(held, filings=[{"desc": "Resignation of Statutory Auditor", "an_dt": "10-Sep-2026 18:40:00"}])
    flags, _ = holdings_news.find()
    assert flags == []


def test_one_event_reported_as_filing_and_headline_alerts_once(held):
    text = "Resignation of Statutory Auditor"
    _sources(held, filings=[{"desc": text, "an_dt": "23-Sep-2026 18:40:00"}],
             headlines=[{"title": text, "published": datetime(2026, 9, 23, 19, 0)}])
    flags, _ = holdings_news.find()
    assert len(flags) == 1


def test_one_source_failing_still_checks_the_other(held):
    _sources(held, headlines=[{"title": "SEBI order against Indian Bank", "published": datetime(2026, 9, 23, 9)}],
             fail=("filings",))
    flags, unchecked = holdings_news.find()
    assert len(flags) == 1
    assert unchecked == []


def test_both_sources_failing_is_reported_not_silent(held):
    _sources(held, fail=("filings", "headlines"))
    flags, unchecked = holdings_news.find()
    assert flags == []
    assert unchecked == ["INDIANB"]


def test_each_item_alerts_once(held):
    from src.alerts import telegram

    delivered: set[str] = set()

    def fake_send(body, *, dedupe_key, **kwargs):
        if dedupe_key in delivered:
            return False
        delivered.add(dedupe_key)
        return True

    held.setattr(telegram, "send", fake_send)
    flag = holdings_news.NewsFlag("INDIANB", "governance", "Auditor resigns", "NSE filing", None, held=30)
    assert holdings_news.send([flag]) == 1
    assert holdings_news.send([flag]) == 0


# --- The summary's news line ------------------------------------------------------


def _summary(**kwargs):
    from types import SimpleNamespace

    from src.alerts import telegram
    from src.config import load_config

    scan = SimpleNamespace(universe_size=501, duration_seconds=500, passed_dip=15,
                           passed_delivery=1, trade_date=date(2026, 9, 24))
    return telegram.scan_summary(scan, [], load_config(), holdings=2, **kwargs)


def test_summary_news_lines():
    assert "No serious news on your holdings" in _summary(news_sent=0, news_unchecked=[])
    assert "1 serious news item on your holdings" in _summary(news_sent=1, news_unchecked=[])
    assert "couldn't run" in _summary(news_sent=0, news_unchecked=None)
    body = _summary(news_sent=0, news_unchecked=["SBIN"])
    assert "Couldn't reach news for: SBIN" in body


# --- Running the morning scan once ------------------------------------------------


@pytest.fixture
def scans(tmp_path, monkeypatch):
    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{tmp_path/'scans.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "_engine", engine)
    db.metadata.create_all(engine)

    def add(trade_date, size, status="complete"):
        with db.connection() as conn:
            conn.execute(db.scans.insert().values(run_at=db.now(), trade_date=trade_date,
                                                  universe="NIFTY 500", status=status,
                                                  universe_size=size))
    return add


def test_a_full_scan_of_the_session_counts(scans):
    from jobs import daily_scan

    scans(date(2026, 9, 23), 501)
    assert daily_scan.already_scanned(date(2026, 9, 23))
    assert not daily_scan.already_scanned(date(2026, 9, 24))


def test_a_test_scan_or_a_failed_one_does_not_count(scans):
    from jobs import daily_scan

    scans(date(2026, 9, 23), 20)
    scans(date(2026, 9, 23), 501, status="failed")
    assert not daily_scan.already_scanned(date(2026, 9, 23))


def test_the_second_trigger_stops_before_doing_anything(scans, monkeypatch):
    from jobs import daily_scan
    from src.data import nse

    scans(date(2026, 9, 23), 501)
    monkeypatch.setattr(nse, "last_completed_session", lambda now=None: date(2026, 9, 23))
    monkeypatch.setattr(daily_scan.scan, "run_scan", lambda **k: pytest.fail("must not rescan"))
    monkeypatch.setattr(daily_scan, "store_todays_delivery", lambda *a, **k: pytest.fail("must not run"))
    monkeypatch.setattr(sys, "argv", ["daily_scan.py", "--once-per-session", "--send-at", "08:00"])
    assert daily_scan.main() == 0


def test_the_workflow_accepts_the_timer_trigger():
    import yaml

    workflow = yaml.safe_load(open(".github/workflows/daily-scan.yml", encoding="utf-8"))
    inputs = workflow[True]["workflow_dispatch"]["inputs"]
    assert inputs["morning"]["type"] == "boolean"
    run_step = next(s for s in workflow["jobs"]["scan"]["steps"] if s.get("name") == "Run the scan")
    assert "github.event.inputs.morning" in run_step["run"]
    assert "--once-per-session" in run_step["run"]
