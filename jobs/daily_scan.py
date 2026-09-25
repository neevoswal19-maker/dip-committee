"""The evening job: scan, assess, check holdings, alert.

Runs on GitHub Actions after NSE publishes the day's bhavcopy. Writes to
Postgres; the dashboard only ever reads.

Ordering is deliberate. Delivery bars are stored *before* the scan so the
screen reads them from the database rather than making sixty NSE requests on
an ephemeral runner with no disk cache. Holdings are checked even when the
scan finds nothing, because an exit signal on something you own matters more
than any new candidate.

Exit codes: 0 success, 1 failure. Actions surfaces a non-zero exit, and a
failure alert goes out too - silence should never be ambiguous between
"nothing found" and "the job never ran".
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
import traceback
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import committee as committee_module
from src import db, portfolio, scan
from src.alerts import holdings_news, telegram, telegram_inbox
from src.strategy import regime as regime_rules
from src.config import load_config
from src.data import nse, prices
from src.data.provider import StockIdentity
from src.strategy import exit as exit_rules
from src.strategy import sizing as sizing_rules

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("daily_scan")


def store_todays_delivery(cfg, index: str | None = None) -> int:
    """Persist the latest session so history accumulates run over run.

    Restricted to the screening universe. The bhavcopy holds every NSE
    equity (~3,500); storing the ~2,900 outside the index costs round trips
    for rows nothing ever reads, and pg8000 charges one round trip per row
    against a database that may be on another continent.
    """
    # The most recent published session. Walk back past holidays: a Thursday
    # morning run after a Wednesday holiday finds Tuesday, which is already
    # stored and skipped row by row, so walking back is always safe.
    trade_date = nse.last_completed_session()
    result = nse.get_bhavcopy(trade_date)
    for _ in range(4):
        if result.value is not None:
            break
        log.info("No bhavcopy for %s (%s); trying the session before", trade_date, result.note)
        trade_date = nse.last_trading_day(trade_date - timedelta(days=1))
        result = nse.get_bhavcopy(trade_date)

    if result.value is None:
        log.warning("No bhavcopy found in the last five sessions")
        return 0

    universe = nse.get_universe(index or cfg.get("universe.index", "NIFTY 500"))
    symbols = {s.symbol for s in (universe.value or [])} or None
    if symbols:
        log.info("Storing delivery for %d universe symbols", len(symbols))

    stored = nse.store_delivery_bars(result.value, trade_date, symbols=symbols)
    log.info("Stored %d delivery bars for %s", stored, trade_date)
    return stored


def check_holdings(cfg) -> list[dict]:
    """Run the exit doctrine over long-term holdings, and alert on what fires.

    Swing positions the owner records are tracked but not run through this:
    trims at +25% and a 20% trailing stop are a long-term plan, and no swing
    rule passed the research, so there is no tested swing exit to apply.
    """
    alerts: list[dict] = []
    states = portfolio.open_positions(cfg=cfg, strategy=portfolio.LONG_TERM)

    if not states:
        log.info("No open positions to check")
        return alerts

    log.info("Checking %d open position(s)", len(states))

    for state in states:
        price_result = prices.get_price_history(StockIdentity(state.symbol), years=1)
        if not price_result.usable or price_result.value is None or price_result.value.empty:
            log.warning("No price for %s, skipping its exit check", state.symbol)
            continue

        price = float(price_result.value["close"].iloc[-1])

        from sqlalchemy import select

        with db.connection() as conn:
            row = conn.execute(
                select(db.positions).where(db.positions.c.id == state.position_id)
            ).first()
        record = dict(row._mapping) if row else {}

        peak = max(float(record.get("peak_price") or 0), price)
        if peak > float(record.get("peak_price") or 0):
            with db.connection() as conn:
                conn.execute(
                    db.positions.update()
                    .where(db.positions.c.id == state.position_id)
                    .values(peak_price=peak, peak_date=date.today())
                )

        position = exit_rules.Position.from_state(
            state, stop_price=record.get("stop_price"), peak_price=peak,
            conviction=record.get("conviction"),
        )
        signals = exit_rules.evaluate(position, price, cfg)

        for signal in signals:
            # Exits and trims always alert. Tax deadlines alert inside their
            # window. A HOLD does not - a daily "nothing to do" message is one
            # you learn to ignore, and then miss the day it says otherwise.
            if signal.action in ("EXIT", "TRIM"):
                body = telegram.exit_signal(state.symbol, signal, position, price, cfg)
                key = f"exit|{state.symbol}|{signal.rule}|{date.today().isoformat()}"
                if telegram.send(body, alert_type="exit_signal", symbol=state.symbol,
                                 dedupe_key=key, cfg=cfg):
                    alerts.append({"symbol": state.symbol, "rule": signal.rule})

            elif signal.rule == "ltcg_deadline":
                body = telegram.ltcg_warning(state.symbol, signal.detail, cfg)
                # Weekly rather than daily: the deadline moves one day at a
                # time and a daily reminder is noise.
                week = date.today().isocalendar()
                key = f"ltcg|{state.symbol}|{week[0]}-W{week[1]}"
                if telegram.send(body, alert_type="ltcg_deadline", symbol=state.symbol,
                                 dedupe_key=key, cfg=cfg):
                    alerts.append({"symbol": state.symbol, "rule": "ltcg_deadline"})

    return alerts


def alert_on_buys(cfg) -> list[str]:
    """Alert on committee BUY verdicts from today's scan.

    Entry alerts fire only on BUY. A WATCH is the committee saying it is not
    convinced, and alerting on it would train you to act on the thing the
    system declined to recommend.
    """
    sent: list[str] = []
    _, candidates = scan.latest_candidates()

    for candidate in candidates:
        run = committee_module.latest_run(candidate["symbol"])
        if not run or run.get("stance") != "BUY":
            continue

        report = committee_module.rebuild_report(run)
        if report is None:
            continue

        body = telegram.buy_candidate(report, cfg)
        key = f"buy|{candidate['symbol']}|{date.today().isoformat()}"
        if telegram.send(body, alert_type="new_buy_candidate",
                         symbol=candidate["symbol"], dedupe_key=key, cfg=cfg):
            sent.append(candidate["symbol"])

    return sent


#: A scan this size is a full Nifty 500 run. A --limit test scan is smaller
#: and must not count as the morning's scan.
FULL_SCAN_MIN_UNIVERSE = 400


def already_scanned(session) -> bool:
    """Whether a full scan of `session` has already completed.

    Two triggers start the morning run - an external timer at 07:40 and
    GitHub's own schedule as a backup, which can arrive hours late - so the
    second one to run must find the work done and stop. Rescanning mid-morning
    would read intraday prices as if they were a closing session.
    """
    from sqlalchemy import select

    with db.connection() as conn:
        row = conn.execute(
            select(db.scans.c.id)
            .where(db.scans.c.status == "complete")
            .where(db.scans.c.trade_date == session)
            .where(db.scans.c.universe_size >= FULL_SCAN_MIN_UNIVERSE)
            .limit(1)
        ).first()
    return row is not None


def hold_until(send_at: str | None) -> None:
    """Sleep until `send_at` (HH:MM, IST) if it is still ahead.

    The scheduled run starts early - GitHub starts cron jobs late, often by
    ten minutes or more, and the scan itself takes about ten - so that every
    message can go out at one fixed time instead of whenever the run happens
    to finish. If the run is already past the time, nothing waits.
    """
    if not send_at:
        return
    hour, minute = (int(part) for part in send_at.split(":"))
    now = db.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    delay = (target - now).total_seconds()
    if delay <= 0:
        log.info("Already past %s IST; sending now", send_at)
        return
    if delay > 3 * 3600:
        # A mistyped time should not park the job until it times out.
        log.warning("%s IST is more than three hours away; sending now instead", send_at)
        return
    log.info("Holding messages until %s IST (%.0f minutes)", send_at, delay / 60)
    time.sleep(delay)


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily scan and alert job")
    parser.add_argument("--index", default=None, help="universe, default from config")
    parser.add_argument("--limit", type=int, default=None, help="cap the universe, for testing")
    parser.add_argument("--no-alerts", action="store_true", help="run without sending anything")
    parser.add_argument("--summary", action="store_true",
                        help="send the morning summary (scheduled runs always do)")
    parser.add_argument("--send-at", default=None, metavar="HH:MM",
                        help="hold every Telegram message until this IST time")
    parser.add_argument("--once-per-session", action="store_true",
                        help="do nothing if this session has already been fully scanned")
    args = parser.parse_args()

    cfg = load_config()
    db.init_db()

    # Fails loudly rather than shipping credentials over plaintext. See
    # db.assert_encrypted for why this cannot be assumed.
    db.assert_encrypted()

    if args.once_per_session:
        session = nse.last_completed_session()
        if already_scanned(session):
            log.info("The %s session has already been scanned; nothing to do", session)
            return 0

    if args.no_alerts:
        telegram.send = lambda *a, **k: False  # type: ignore[assignment]
        log.info("Alerts disabled for this run")
    elif not telegram.configured():
        log.warning("Telegram is not configured; the job will run but stay silent")

    try:
        log.info("--- storing today's delivery")
        store_todays_delivery(cfg, args.index)

        log.info("--- scanning")
        summary = scan.run_scan(
            index=args.index, limit=args.limit, check_quality=True,
            run_committee=True, cfg=cfg,
        )
        log.info(
            "Scanned %d in %.0fs: %d dip, %d quality, %d delivery, %d candidates",
            summary.universe_size, summary.duration_seconds, summary.passed_dip,
            summary.passed_quality, summary.passed_delivery, summary.candidates,
        )

        # Trades reported by message since the last run, so the exit rules
        # below see what is actually held.
        log.info("--- Telegram inbox")
        if args.no_alerts:
            # With sending switched off, the confirmations would be dropped
            # while the trades were still recorded - the owner would never
            # know. A silent run leaves their messages for the inbox job.
            log.info("Skipped: alerts are off, so replies could not be sent")
        else:
            telegram_inbox.process_pending(cfg)

        # Fetched now, sent after the hold with the other alerts. A failure
        # here must not cost the morning's scan.
        log.info("--- news on holdings")
        try:
            news_flags, news_unchecked = holdings_news.find(cfg)
        except Exception:
            log.exception("The holdings news check failed")
            news_flags, news_unchecked = [], None
        log.info("%d serious item(s) found on holdings", len(news_flags))

        # Everything from here is built now and posted at the send time.
        if args.send_at:
            telegram.hold_messages()

        log.info("--- alerting on BUY verdicts")
        buys = alert_on_buys(cfg)
        log.info("Sent %d buy alert(s): %s", len(buys), buys or "none")

        log.info("--- checking holdings")
        exits = check_holdings(cfg)
        log.info("Sent %d holding alert(s)", len(exits))

        news_sent = holdings_news.send(news_flags, cfg)
        log.info("Sent %d holding news alert(s)", news_sent)

        log.info("--- market regime")
        market = regime_rules.current(cfg)
        log.info(regime_rules.describe_for_humans(market))
        change = regime_rules.downtrend_transition(cfg)
        if change is not None:
            previous, current_regime = change
            telegram.send(
                telegram.regime_change(previous, current_regime, cfg),
                alert_type="regime_change",
                dedupe_key=f"regime|{current_regime.label}|{current_regime.as_of}",
                cfg=cfg,
            )

        if args.summary:
            _, candidates = scan.latest_candidates()
            convictions = scan.convictions_for([c["symbol"] for c in candidates])
            enriched = [
                {
                    **c,
                    "conviction": convictions.get(c["symbol"], {}).get("conviction"),
                    "stance": convictions.get(c["symbol"], {}).get("stance"),
                }
                for c in candidates
            ]
            telegram.send(
                telegram.scan_summary(
                    summary, enriched, cfg, market=market.to_dict(),
                    buy_alerts=buys, holding_alerts=len(exits),
                    holdings=len(portfolio.open_positions(cfg=cfg, strategy=portfolio.LONG_TERM)),
                    swing_holdings=len(portfolio.open_positions(cfg=cfg, strategy=portfolio.SWING)),
                    news_sent=news_sent, news_unchecked=news_unchecked,
                ),
                alert_type="scan_summary",
                dedupe_key=f"summary|{date.today().isoformat()}",
                cfg=cfg,
            )

        if args.send_at:
            hold_until(args.send_at)
            log.info("Released %d message(s)", telegram.release_messages())

        log.info("Done.")
        return 0

    except Exception as exc:
        log.exception("The daily scan failed")
        if not args.no_alerts:
            hold_until(args.send_at)
            # Anything built before the failure goes out with the failure notice.
            telegram.release_messages()
            telegram.send(
                telegram.failure(f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()[-800:]}"),
                alert_type="scan_failure",
                dedupe_key=f"failure|{date.today().isoformat()}",
                cfg=cfg,
            )
        return 1


if __name__ == "__main__":
    sys.exit(main())
