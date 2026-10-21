"""The intraday ORB + VWAP pullback strategy, live, in one run from 9:15 to 15:12.

About 20 seconds after each 5-minute candle closes it refreshes NIFTYBEES and
BANKBEES from Yahoo, runs src/intraday/orb.py on the day's completed bars,
sends any new event to Telegram (INTRADAY - PAPER while intraday.mode is
paper) and keeps the day's trade in `intraday_trades`. At the end it stores
the day's bars and sends a wrap-up with the running 40-trade test.

Separate from the dip strategy: it reads and writes only intraday tables and
alerts_sent. Started by an external timer at 9:25 IST (GitHub's own schedule
runs late); a second start the same day exits at once.
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
import time as clock
from datetime import datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from src import db
from src.alerts import telegram
from src.config import load_config
from src.intraday import data, orb

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("intraday_live")

OPEN, HOLIDAY_CHECK = time(9, 15), time(9, 40)
NOTIFY = {"armed", "cancelled", "expired", "entry", "partial", "exit"}


def claim_session(day, mode: str) -> bool:
    """One run a day: the first start claims it, any later start exits."""
    try:
        with db.connection() as conn:
            conn.execute(db.alerts_sent.insert().values(
                sent_at=db.now(), channel="internal", alert_type="intraday_session",
                dedupe_key=f"intraday_session|{mode}|{day.isoformat()}", body="", delivered=True))
        return True
    except IntegrityError:
        return False


def save_trade(trade: orb.DayTrade, mode: str, costs: dict) -> dict:
    r = orb.result(trade, costs)
    values = {"side": r["side"], "entry_at": trade.entry_at.to_pydatetime(), "entry": r["entry"],
              "stop": r["stop"], "units": r["units"], "exit_reason": r["exit_reason"], "gross": r["gross"],
              "charges": r["charges"], "net": r["net"], "r": r["r"], "closed": r["closed"], "updated_at": db.now()}
    t = db.intraday_trades
    with db.connection() as conn:
        done = conn.execute(update(t).where(t.c.day == trade.day).where(t.c.symbol == trade.symbol)
                            .where(t.c.mode == mode).values(**values)).rowcount
        if not done:
            conn.execute(t.insert().values(day=trade.day, symbol=trade.symbol, mode=mode, **values))
    return r


def paper_rows(mode: str) -> list[dict]:
    with db.connection() as conn:
        rows = [dict(r._mapping) for r in conn.execute(
            select(db.intraday_trades).where(db.intraday_trades.c.mode == mode).order_by(db.intraday_trades.c.entry_at))]
    for row in rows:
        row["closed"] = bool(row.get("closed"))
    return rows


def next_wake(now: datetime) -> datetime:
    """20 seconds after the next 5-minute boundary."""
    base = now.replace(second=0, microsecond=0)
    minutes = 5 - base.minute % 5
    return base + timedelta(minutes=minutes, seconds=20)


def deadline_for(now: datetime, until: time, max_minutes: float) -> datetime:
    """When the loop must stop: the configured end, or earlier if GitHub's
    six-hour limit would otherwise cut the run off first."""
    return min(datetime.combine(now.date(), until), now + timedelta(minutes=max_minutes))


def main() -> int:
    parser = argparse.ArgumentParser(description="Intraday ORB + VWAP pullback, live")
    parser.add_argument("--no-alerts", action="store_true")
    parser.add_argument("--until", default="15:12", help="IST time to stop, HH:MM")
    args = parser.parse_args()

    cfg = load_config()
    ic = cfg.get("intraday", {}) or {}
    mode = str(ic.get("mode", "paper"))
    names = data.symbols(cfg)
    costs = ic.get("costs", {}) or {}
    until = time(*map(int, args.until.split(":")))
    db.init_db()
    db.assert_encrypted()

    now = db.now()
    day = now.date()
    # GitHub stops a job after 6 hours. Finish (and send the wrap-up) before
    # that, whatever time the timer started it: on 19 and 21 October a 9:12
    # start was cut off at 15:10, before the wrap-up and the bar store.
    deadline = deadline_for(now, until, float((cfg.get("intraday", {}) or {}).get("max_job_minutes", 345)))
    if deadline.time() < time(15, 11):
        log.warning("Started at %s: the run must end by %s, before the 15:10 square-off. Start it at 9:25.",
                    now.strftime("%H:%M"), deadline.strftime("%H:%M"))
    if now.weekday() >= 5:
        log.info("Weekend; nothing to do")
        return 0
    if now.time() >= until:
        log.info("Started after %s; nothing left to trade today", until)
        return 0
    if not claim_session(day, mode):
        log.info("Today's session is already running or done")
        return 0
    if args.no_alerts:
        telegram.send = lambda *a, **k: False  # type: ignore[assignment]
        log.info("Alerts disabled for this run")

    # Seed the stored history once, so warm-up and backtests have it.
    stored = data.load(names, since=datetime.combine(day - timedelta(days=10), time(0)))
    if not stored:
        log.info("Seeding intraday_bars with Yahoo's 60 sessions: %d bars", data.store(data.fetch(names, "60d")))
        stored = data.load(names, since=datetime.combine(day - timedelta(days=10), time(0)))

    tracks = {n: (ic.get("symbols", {}) or {}).get(n, {}).get("tracks") for n in names}
    sent: set[str] = set()
    delays: list[float] = []
    traded: dict[str, dict] = {}

    while True:
        now = db.now()
        if now >= deadline:
            break
        wake = min(next_wake(now), deadline)
        clock.sleep(max(0.0, (wake - db.now()).total_seconds()))
        now = db.now()
        if now.time() < OPEN:
            continue

        try:
            fresh = data.fetch(names, period="5d")
        except Exception as exc:              # one failed fetch must not end the day
            log.warning("Fetch failed at %s: %s", now.strftime("%H:%M"), exc)
            continue

        any_today = False
        for name in names:
            bars = data.completed(data.merge(stored, fresh).get(name), now)
            if bars is None or bars.empty:
                continue
            today = bars[bars.index.date == day]
            if today.empty:
                continue
            any_today = True
            lag = (now - (today.index.max() + timedelta(minutes=5)).to_pydatetime()).total_seconds() / 60.0
            delays.append(lag)
            if lag > float(ic.get("max_bar_delay_minutes", 2)):
                log.warning("%s: the newest completed bar closed %.1f min ago - Yahoo is running late", name, lag)

            f = orb.with_indicators(data.recent(bars))
            events, trade = orb.session_events(f, day, name, orb.params_for(name, cfg))
            for event in events:
                if event.key() in sent:
                    continue
                sent.add(event.key())
                log.info("%s %s %s %s", name, event.kind, event.side or "", event.detail)
                body = telegram.intraday_event(event, mode, tracks.get(name)) if event.kind in NOTIFY else None
                if body:
                    telegram.send(body, alert_type=f"intraday_{event.kind}", symbol=name,
                                  dedupe_key=f"intraday|{mode}|{event.key()}")
            if trade is not None:
                traded[name] = save_trade(trade, mode, costs)

        if not any_today and now.time() >= HOLIDAY_CHECK:
            log.info("No bars for today by %s: a market holiday", HOLIDAY_CHECK)
            return 0

    # --- Close of day
    try:
        written = data.store({n: data.completed(f, db.now()) for n, f in data.fetch(names, "5d").items()})
        log.info("Stored %d bars", written)
    except Exception as exc:
        log.warning("Could not store today's bars: %s", exc)

    rows = paper_rows(mode)
    card = orb.scorecard(rows, float(ic.get("capital", 500000)))
    verdict, why = orb.judge(card)
    max_delay = max(delays) if delays else None
    if max_delay is not None and not math.isnan(max_delay):
        log.info("Bars arrived up to %.1f min after the candle closed", max_delay)
    body = telegram.intraday_day_summary(day, list(traded.values()), card, verdict, why, max_delay, mode)
    telegram.send(body, alert_type="intraday_summary", dedupe_key=f"intraday_summary|{mode}|{day.isoformat()}")
    log.info("Done: %d trades today; paper %s", len(traded), card)
    return 0


if __name__ == "__main__":
    sys.exit(main())
