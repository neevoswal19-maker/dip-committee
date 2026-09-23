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
import traceback
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import committee as committee_module
from src import db, portfolio, scan
from src.alerts import telegram, telegram_inbox
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
    trade_date = nse.last_trading_day()
    result = nse.get_bhavcopy(trade_date)

    if result.value is None:
        log.warning("No bhavcopy for %s (%s)", trade_date, result.note)
        return 0

    universe = nse.get_universe(index or cfg.get("universe.index", "NIFTY 500"))
    symbols = {s.symbol for s in (universe.value or [])} or None
    if symbols:
        log.info("Storing delivery for %d universe symbols", len(symbols))

    stored = nse.store_delivery_bars(result.value, trade_date, symbols=symbols)
    log.info("Stored %d delivery bars for %s", stored, trade_date)
    return stored


def check_holdings(cfg) -> list[dict]:
    """Run the exit doctrine over everything held, and alert on what fires."""
    alerts: list[dict] = []
    states = portfolio.open_positions(cfg=cfg)

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


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily scan and alert job")
    parser.add_argument("--index", default=None, help="universe, default from config")
    parser.add_argument("--limit", type=int, default=None, help="cap the universe, for testing")
    parser.add_argument("--no-alerts", action="store_true", help="run without sending anything")
    parser.add_argument("--summary", action="store_true",
                        help="send a summary even when there is nothing to report")
    args = parser.parse_args()

    cfg = load_config()
    db.init_db()

    # Fails loudly rather than shipping credentials over plaintext. See
    # db.assert_encrypted for why this cannot be assumed.
    db.assert_encrypted()

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

        log.info("--- alerting on BUY verdicts")
        buys = alert_on_buys(cfg)
        log.info("Sent %d buy alert(s): %s", len(buys), buys or "none")

        # Trades reported by message since the last run, so the exit rules
        # below see what is actually held.
        log.info("--- Telegram inbox")
        telegram_inbox.process_pending(cfg)

        log.info("--- checking holdings")
        exits = check_holdings(cfg)
        log.info("Sent %d holding alert(s)", len(exits))

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

        if args.summary and not buys and not exits:
            _, candidates = scan.latest_candidates()
            convictions = scan.convictions_for([c["symbol"] for c in candidates])
            enriched = [
                {**c, "conviction": convictions.get(c["symbol"], {}).get("conviction")}
                for c in candidates
            ]
            telegram.send(
                telegram.scan_summary(summary, enriched, cfg, market=market.to_dict()),
                alert_type="scan_summary",
                dedupe_key=f"summary|{date.today().isoformat()}",
                cfg=cfg,
            )

        log.info("Done.")
        return 0

    except Exception as exc:
        log.exception("The daily scan failed")
        if not args.no_alerts:
            telegram.send(
                telegram.failure(f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()[-800:]}"),
                alert_type="scan_failure",
                dedupe_key=f"failure|{date.today().isoformat()}",
                cfg=cfg,
            )
        return 1


if __name__ == "__main__":
    sys.exit(main())
