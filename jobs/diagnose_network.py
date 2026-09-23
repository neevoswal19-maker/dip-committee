"""Can this machine actually reach the data sources?

Written after the first GitHub Actions run hung for 29 minutes on its first
NSE request and was killed by the job timeout. The question that needs a
definitive answer is whether NSE serves datacenter IPs at all - if it does
not, the scheduled scan cannot live on a hosted runner and the architecture
has to change.

Every request here carries a hard timeout and reports what happened, so a
hang is distinguishable from a block, a block from a slow response, and all
three from something being wrong in our own code.
"""

from __future__ import annotations

import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TIMEOUT = 20


def timed(label: str, fn) -> tuple[bool, float, str]:
    start = time.time()
    try:
        detail = fn()
        return True, time.time() - start, detail
    except Exception as exc:
        return False, time.time() - start, f"{type(exc).__name__}: {str(exc)[:110]}"


def check(label: str, fn) -> bool:
    ok, seconds, detail = timed(label, fn)
    mark = "ok  " if ok else "FAIL"
    print(f"  {mark} {label:<34} {seconds:6.1f}s  {detail}", flush=True)
    return ok


def main() -> int:
    print("Where am I?")
    try:
        import httpx

        info = httpx.get("https://ipinfo.io/json", timeout=TIMEOUT).json()
        print(f"  ip {info.get('ip')}  {info.get('city')}, {info.get('country')}  {info.get('org')}")
    except Exception as exc:
        print(f"  could not determine: {exc}")

    print("\nDNS")
    for host in ("www.nseindia.com", "nsearchives.nseindia.com", "query1.finance.yahoo.com"):
        check(host, lambda h=host: socket.gethostbyname(h))

    print("\nPlain TCP on 443")
    for host in ("www.nseindia.com", "nsearchives.nseindia.com"):
        def connect(h=host):
            sock = socket.create_connection((h, 443), timeout=TIMEOUT)
            sock.close()
            return "connected"
        check(f"{host}:443", connect)

    print("\nNSE over curl_cffi (what the app uses)")
    from curl_cffi import requests as creq

    session = creq.Session(impersonate="chrome")

    def homepage():
        r = session.get("https://www.nseindia.com/", timeout=TIMEOUT)
        return f"HTTP {r.status_code}, {len(r.content)} bytes, {len(session.cookies)} cookies"

    warm = check("homepage (cookie warm-up)", homepage)

    def constituents():
        r = session.get(
            "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv",
            timeout=TIMEOUT,
        )
        return f"HTTP {r.status_code}, {len(r.content)} bytes"

    check("ind_nifty50list.csv", constituents)

    def bhavcopy():
        from datetime import date, timedelta

        day = date.today()
        while day.weekday() >= 5:
            day -= timedelta(days=1)
        url = (
            "https://nsearchives.nseindia.com/products/content/"
            f"sec_bhavdata_full_{day:%d%m%Y}.csv"
        )
        r = session.get(url, timeout=TIMEOUT)
        return f"HTTP {r.status_code}, {len(r.content)} bytes ({day})"

    check("bhavcopy (the step that hung)", bhavcopy)

    print("\nYahoo Finance")

    def yahoo():
        import yfinance as yf

        df = yf.download("RELIANCE.NS", period="5d", progress=False,
                         auto_adjust=False, multi_level_index=False)
        return f"{len(df)} rows"

    check("yfinance RELIANCE.NS", yahoo)

    print("\nDatabase")

    def database():
        from src import db

        db.init_db()
        return f"{db.get_engine().url.get_backend_name()}, TLS {db.assert_encrypted()}"

    check("Neon Postgres", database)

    print("\n" + "=" * 70)
    if not warm:
        print("NSE did not answer. If this is a hosted runner, the scheduled scan")
        print("cannot fetch NSE data from here and the design needs to change.")
        return 1
    print("NSE answered. The hang was not a blanket block.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
