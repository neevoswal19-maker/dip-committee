"""Timestamps written from different machines must agree.

The daily job runs on a GitHub runner in UTC, the dashboard on Streamlit Cloud
in UTC, and local work on a PC in IST, all against one database. With plain
`datetime.now()` a local test scan at 16:42 IST sorted as newer than the
evening's real 501-stock run at 13:17 UTC, so both the dashboard and the BUY
alert step read the wrong scan.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src import db, scan
from src.agents.schemas import CommitteeReport


def test_now_is_ist_whatever_the_machine_clock():
    expected = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    got = db.now()
    assert got.tzinfo is None
    assert abs((got - expected.replace(tzinfo=None)).total_seconds()) < 2


def test_committee_reports_are_stamped_in_ist():
    report = CommitteeReport(symbol="TEST")
    assert abs((report.run_at - db.now()).total_seconds()) < 2


class TestLatestIsByInsertionOrder:
    @pytest.fixture(autouse=True)
    def clean_db(self, tmp_path, monkeypatch):
        from sqlalchemy import create_engine

        engine = create_engine(
            f"sqlite:///{tmp_path/'clock.db'}", connect_args={"check_same_thread": False}
        )
        monkeypatch.setattr(db, "_engine", engine)
        db.metadata.create_all(engine)
        yield

    def test_latest_scan_is_the_newest_row_not_the_latest_clock(self):
        """Reproduces the real incident with its real timestamps."""
        with db.connection() as conn:
            # Written first, from the IST machine.
            conn.execute(db.scans.insert().values(
                run_at=datetime(2026, 9, 23, 16, 42), universe="NIFTY 500",
                status="complete", universe_size=120))
            # Written later, from a UTC runner, with an *earlier-looking* stamp.
            conn.execute(db.scans.insert().values(
                run_at=datetime(2026, 9, 23, 13, 17), universe="NIFTY 500",
                status="complete", universe_size=501))

        latest = scan.latest_scan()
        assert latest["universe_size"] == 501
        assert [s["universe_size"] for s in scan.scan_history()] == [501, 120]

    def test_latest_committee_run_is_the_newest_row(self):
        from src import committee

        with db.connection() as conn:
            for stamp, conviction in ((datetime(2026, 9, 23, 16, 42), 40.0),
                                      (datetime(2026, 9, 23, 13, 17), 70.0)):
                conn.execute(db.committee_runs.insert().values(
                    symbol="TEST", run_at=stamp, stance="WATCH", conviction=conviction))

        assert committee.latest_run("TEST")["conviction"] == 70.0
        assert scan.convictions_for(["TEST"])["TEST"]["conviction"] == 70.0
