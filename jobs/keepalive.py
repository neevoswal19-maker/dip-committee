"""A standalone liveness check for the deployed system.

Separate from the keepalive *workflow*, which only stops GitHub disabling the
cron. This answers a different question: is the deployed system still working?

Run it manually, or point an uptime monitor at it. It checks the database is
reachable, the scan has run recently, and Telegram is configured - and says
which of those is false rather than just failing.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db, scan
from src.alerts import telegram
from src.config import database_url


def main() -> int:
    problems: list[str] = []

    try:
        db.init_db()
        backend = db.get_engine().url.get_backend_name()
        print(f"database      OK ({backend})")
        if backend != "sqlite":
            try:
                db.assert_encrypted()
                print("encryption    TLS verified on the live connection")
            except RuntimeError as exc:
                problems.append(str(exc))
                print("encryption    NOT ENCRYPTED")
        if backend == "sqlite":
            problems.append(
                "running on SQLite - in a deployed environment this is ephemeral "
                "and everything written will be lost on restart"
            )
    except Exception as exc:
        problems.append(f"database unreachable: {exc}")
        print(f"database      FAILED: {exc}")

    try:
        latest = scan.latest_scan()
        if latest is None:
            problems.append("no completed scan has ever been recorded")
            print("last scan     NONE")
        else:
            age = datetime.now() - latest["run_at"]
            print(f"last scan     {latest['run_at']:%d %b %H:%M} ({age.days}d ago), "
                  f"{latest['universe_size']} scanned")
            if age > timedelta(days=4):
                problems.append(f"the last scan was {age.days} days ago - the cron may be disabled")
    except Exception as exc:
        problems.append(f"could not read scans: {exc}")

    if telegram.configured():
        print("telegram      configured")
    else:
        problems.append("Telegram is not configured, so the job runs silently")
        print("telegram      NOT CONFIGURED")

    if problems:
        print("\nProblems:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
