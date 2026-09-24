"""The scan runs before the market opens and delivers at a fixed time.

At 07:17 IST "today" has no data yet. Code that assumes it does either finds
nothing (the delivery store) or asks NSE for a missing file once per stock
(the history walk, since a 404 is not cached). These tests pin the morning
behaviour, and the hold that makes every message arrive at 08:00.
"""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

from jobs import daily_scan
from src import db
from src.data import nse
from src.data.provider import DataResult, DataStatus


@pytest.mark.parametrize("now, expected", [
    (datetime(2026, 9, 24, 7, 17), date(2026, 9, 23)),    # Thu morning -> Wed
    (datetime(2026, 9, 24, 18, 59), date(2026, 9, 23)),   # not yet published
    (datetime(2026, 9, 24, 19, 0), date(2026, 9, 24)),    # published
    (datetime(2026, 9, 28, 7, 17), date(2026, 9, 25)),    # Mon morning -> Fri
    (datetime(2026, 9, 26, 10, 0), date(2026, 9, 25)),    # Saturday -> Fri
    (datetime(2026, 9, 27, 21, 0), date(2026, 9, 25)),    # Sunday night -> Fri
])
def test_last_completed_session(now, expected):
    assert nse.last_completed_session(now) == expected


def test_the_history_walk_does_not_ask_for_todays_missing_file(monkeypatch):
    monkeypatch.setattr(db, "now", lambda: datetime(2026, 9, 24, 7, 17))
    asked: list[date] = []

    def fake_bhavcopy(trade_date, **kwargs):
        asked.append(trade_date)
        return DataResult.empty("nse", "test")

    monkeypatch.setattr(nse, "get_bhavcopy", fake_bhavcopy)
    nse.get_delivery_history("INDIANB", days=3, use_database=False)

    assert asked[0] == date(2026, 9, 23)
    assert date(2026, 9, 24) not in asked


class TestDeliveryStore:
    @pytest.fixture
    def stored(self, monkeypatch):
        monkeypatch.setattr(db, "now", lambda: datetime(2026, 9, 24, 7, 17))
        record: dict = {}

        def fake_store(frame, trade_date, symbols=None):
            record["date"] = trade_date
            return len(frame)

        monkeypatch.setattr(nse, "store_delivery_bars", fake_store)
        monkeypatch.setattr(nse, "get_universe", lambda index: DataResult(
            value=[], status=DataStatus.OK, source="test"))
        return record

    def test_stores_the_previous_session_in_the_morning(self, monkeypatch, stored):
        monkeypatch.setattr(nse, "get_bhavcopy", lambda d, **k: DataResult(
            value=pd.DataFrame({"SYMBOL": ["INDIANB"]}), status=DataStatus.OK, source="test"))
        assert daily_scan.store_todays_delivery(cfg=_Cfg()) == 1
        assert stored["date"] == date(2026, 9, 23)

    def test_walks_back_past_a_holiday(self, monkeypatch, stored):
        def fake_bhavcopy(trade_date, **kwargs):
            if trade_date == date(2026, 9, 23):             # Wednesday: holiday
                return DataResult.empty("nse", "holiday")
            return DataResult(value=pd.DataFrame({"SYMBOL": ["INDIANB"]}),
                              status=DataStatus.OK, source="test")

        monkeypatch.setattr(nse, "get_bhavcopy", fake_bhavcopy)
        daily_scan.store_todays_delivery(cfg=_Cfg())
        assert stored["date"] == date(2026, 9, 22)

    def test_gives_up_quietly_when_nothing_is_published(self, monkeypatch, stored):
        monkeypatch.setattr(nse, "get_bhavcopy", lambda d, **k: DataResult.empty("nse", "down"))
        assert daily_scan.store_todays_delivery(cfg=_Cfg()) == 0
        assert "date" not in stored


class _Cfg:
    def get(self, key, default=None):
        return default


class TestHoldUntil:
    @pytest.fixture
    def slept(self, monkeypatch):
        naps: list[float] = []
        monkeypatch.setattr(daily_scan.time, "sleep", naps.append)
        return naps

    def test_waits_until_the_send_time(self, monkeypatch, slept):
        monkeypatch.setattr(db, "now", lambda: datetime(2026, 9, 24, 7, 29, 30))
        daily_scan.hold_until("08:00")
        assert slept == [pytest.approx(30.5 * 60)]

    def test_sends_immediately_when_already_late(self, monkeypatch, slept):
        monkeypatch.setattr(db, "now", lambda: datetime(2026, 9, 24, 8, 12))
        daily_scan.hold_until("08:00")
        assert slept == []

    def test_a_manual_run_never_waits(self, monkeypatch, slept):
        monkeypatch.setattr(db, "now", lambda: datetime(2026, 9, 24, 7, 0))
        daily_scan.hold_until(None)
        assert slept == []

    def test_a_distant_time_is_treated_as_a_mistake(self, monkeypatch, slept):
        monkeypatch.setattr(db, "now", lambda: datetime(2026, 9, 24, 1, 0))
        daily_scan.hold_until("08:00")
        assert slept == []


def test_the_workflow_runs_before_eight_and_holds_until_eight():
    import yaml

    workflow = yaml.safe_load(open(".github/workflows/daily-scan.yml", encoding="utf-8"))
    cron = workflow[True]["schedule"][0]["cron"]              # PyYAML reads "on" as True
    minute, hour, *_rest, weekdays = cron.split()
    ist_minutes = (int(hour) * 60 + int(minute) + 330) % (24 * 60)
    assert 7 * 60 <= ist_minutes < 8 * 60, "must start between 07:00 and 08:00 IST"
    assert weekdays == "1-5"

    run_step = next(s for s in workflow["jobs"]["scan"]["steps"] if s.get("name") == "Run the scan")
    assert "--send-at 08:00" in run_step["run"]
    assert "schedule" in run_step["run"]                      # manual runs don't wait
    assert workflow["jobs"]["scan"]["timeout-minutes"] >= 60


# --- The morning summary ---------------------------------------------------------


def _summary_inputs():
    from types import SimpleNamespace

    scan = SimpleNamespace(universe_size=501, duration_seconds=500, passed_dip=15,
                           passed_delivery=1, trade_date=date(2026, 9, 24))
    market = {"label": "DOWNTREND", "pct_vs_dma": -0.3, "dma_slope_pct": -1.1}
    return scan, market


def test_summary_names_the_session_and_each_verdict():
    from src.alerts import telegram
    from src.config import load_config

    scan, market = _summary_inputs()
    body = telegram.scan_summary(
        scan, [{"symbol": "INDIANB", "close": 842.0, "drawdown_pct": 15.1,
                "conviction": 53.0, "stance": "WATCH"}],
        load_config(), market=market, holdings=2,
    )
    assert "Thu 24 Sep session" in body
    assert "INDIANB" in body and "WATCH 53" in body
    assert "DOWNTREND" in body
    assert "No exit or tax rules fired on your 2 holdings" in body


def test_summary_points_to_the_alerts_already_sent():
    from src.alerts import telegram
    from src.config import load_config

    scan, market = _summary_inputs()
    body = telegram.scan_summary(scan, [], load_config(), market=market,
                                 buy_alerts=["SBIN"], holding_alerts=1, holdings=1)
    assert "BUY alert sent above: SBIN" in body
    assert "1 alert on your 1 holding" in body


def test_an_unassessed_candidate_says_so():
    from src.alerts import telegram
    from src.config import load_config

    scan, market = _summary_inputs()
    body = telegram.scan_summary(scan, [{"symbol": "SBIN", "close": 800.0, "drawdown_pct": 12.0,
                                         "conviction": None}], load_config(), market=market)
    assert "not assessed" in body


def test_the_scheduled_run_always_summarises():
    import yaml

    workflow = yaml.safe_load(open(".github/workflows/daily-scan.yml", encoding="utf-8"))
    run_step = next(s for s in workflow["jobs"]["scan"]["steps"] if s.get("name") == "Run the scan")
    assert "--send-at 08:00 --summary" in run_step["run"]
