"""Telegram alerts.

The scheduled job runs while you are not watching, so these messages are the
only evidence it ran at all. Two consequences shape the design:

**Deduplication is a database constraint, not a check.** Every alert carries a
key made from its type, symbol and trade date, and `alerts_sent.dedupe_key` is
unique. A re-run inserts, hits the constraint, and skips - so the guard holds
even if two jobs overlap, which a read-then-write check would not.

**Failure has to be loud in the log and quiet in the pipeline.** A Telegram
outage must not fail a scan that otherwise succeeded, but it also must not
pass silently, or you would read the absence of alerts as "nothing happened".
Every attempt is recorded with its outcome.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

import httpx
from sqlalchemy.exc import IntegrityError

from src import db
from src.config import load_config, telegram_credentials

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/sendMessage"

#: Telegram rejects messages over 4096 characters outright.
MAX_LENGTH = 4000


def configured() -> bool:
    token, chat_id = telegram_credentials()
    return bool(token and chat_id)


def _escape(text: Any) -> str:
    """Escape the three characters Telegram's HTML parser cares about."""
    return (
        str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def send(
    body: str,
    *,
    alert_type: str = "info",
    symbol: str | None = None,
    dedupe_key: str | None = None,
    cfg: Any = None,
) -> bool:
    """Send one message, recording the attempt.

    Returns True only when Telegram accepted it. A duplicate returns False
    without sending, which is a success from the caller's point of view - the
    message was already delivered on an earlier run.
    """
    cfg = cfg or load_config()
    token, chat_id = telegram_credentials()

    if not (token and chat_id):
        log.warning("Telegram is not configured; dropping %s alert for %s", alert_type, symbol)
        return False

    key = dedupe_key or f"{alert_type}|{symbol or '-'}|{date.today().isoformat()}"
    db.init_db()

    # Claim the key first. If the insert fails the alert has already gone out,
    # and claiming before sending means a crash mid-send cannot produce a
    # duplicate on the retry.
    try:
        with db.connection() as conn:
            conn.execute(
                db.alerts_sent.insert().values(
                    sent_at=datetime.now(), channel="telegram", alert_type=alert_type,
                    symbol=symbol, dedupe_key=key, body=body[:4000], delivered=False,
                )
            )
    except IntegrityError:
        log.info("Alert %s already sent, skipping", key)
        return False

    text = body if len(body) <= MAX_LENGTH else body[: MAX_LENGTH - 20] + "\n... (truncated)"

    try:
        response = httpx.post(
            API.format(token=token),
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=20.0,
        )
        response.raise_for_status()
    except Exception as exc:
        log.error("Telegram send failed for %s: %s", key, exc)
        with db.connection() as conn:
            conn.execute(
                db.alerts_sent.update()
                .where(db.alerts_sent.c.dedupe_key == key)
                .values(error=str(exc)[:500])
            )
        return False

    with db.connection() as conn:
        conn.execute(
            db.alerts_sent.update()
            .where(db.alerts_sent.c.dedupe_key == key)
            .values(delivered=True)
        )
    log.info("Sent %s alert for %s", alert_type, symbol or "-")
    return True


# --- Message builders -------------------------------------------------------
# Kept separate from sending so they can be tested without a network call,
# and so the wording lives in one place rather than inside the job.


def _disclaimer(cfg: Any) -> str:
    return cfg.get(
        "alerts.disclaimer",
        "Research output, not investment advice. Not a SEBI-registered adviser.",
    )


def buy_candidate(report: Any, cfg: Any = None) -> str:
    """A committee BUY verdict."""
    cfg = cfg or load_config()
    sizing = report.sizing or {}

    lines = [
        f"<b>BUY candidate: {_escape(report.symbol)}</b>",
        f"Conviction <b>{report.conviction:.0f}</b>/100 &middot; {_escape(report.sector or '')}",
        f"Price Rs {report.price:,.2f}",
        "",
    ]

    if sizing.get("is_buy"):
        lines.append(
            f"<b>{_escape(sizing['recommendation'])}</b> - Rs {sizing['total_value']:,.0f} "
            f"({sizing['total_shares']:,} shares, {sizing.get('pct_of_capital', 0):.1f}% of capital)"
        )
        if sizing.get("stop_price"):
            lines.append(
                f"Stop Rs {sizing['stop_price']:,.2f}, risking Rs {sizing.get('risk_amount', 0):,.0f} "
                f"({sizing.get('risk_pct_of_capital', 0):.2f}%)"
            )
        tranches = sizing.get("tranches") or []
        if tranches:
            first = tranches[0]
            lines.append(
                f"Staged: Rs {first['value']:,.0f} now, {len(tranches) - 1} tranche(s) to follow"
            )

    desks = sorted(report.desks, key=lambda d: -abs(d.score) * max(d.confidence, 0.01))
    if desks:
        lines.append("")
        lines.append("<b>Desks</b>")
        for desk in desks[:3]:
            lines.append(f"  {_escape(desk.name)}: {desk.score:+.2f}")

    if report.red_flags:
        lines.append("")
        lines.append(f"<b>{len(report.red_flags)} red flag(s)</b>")
        for flag in report.red_flags[:3]:
            lines.append(f"  - {_escape(flag[:110])}")

    if report.coverage < 0.8:
        lines.append("")
        lines.append(
            f"Data coverage {report.coverage:.0%} - this verdict rests on a narrower "
            f"base than usual."
        )

    lines.extend(["", f"<i>{_escape(_disclaimer(cfg))}</i>"])
    return "\n".join(lines)


def exit_signal(symbol: str, signal: Any, position: Any, price: float, cfg: Any = None) -> str:
    """An exit doctrine signal on something held."""
    cfg = cfg or load_config()
    gain = position.gain_pct(price)

    lines = [
        f"<b>{_escape(signal.action)}: {_escape(symbol)}</b>",
        f"{_escape(signal.rule.replace('_', ' '))} &middot; {gain:+.1f}% at Rs {price:,.2f}",
        "",
        _escape(signal.message),
    ]

    if signal.trim_pct:
        lines.append("")
        lines.append(f"Suggested trim: {signal.trim_pct:.0f}% of the position")

    lines.extend(["", f"<i>{_escape(_disclaimer(cfg))}</i>"])
    return "\n".join(lines)


def ltcg_warning(symbol: str, detail: dict[str, Any], cfg: Any = None) -> str:
    """The short-term to long-term boundary approaching on a specific lot."""
    cfg = cfg or load_config()

    lines = [
        f"<b>Tax deadline: {_escape(symbol)}</b>",
        f"{detail['days_to_ltcg']} days to long-term treatment on the oldest shares.",
        "",
        f"Unrealised profit on that lot: Rs {detail['unrealised_profit']:,.0f}",
        f"Selling now: Rs {detail['stcg_due']:,.0f} tax",
        f"After the anniversary: Rs {detail['ltcg_due']:,.0f}",
        f"<b>Difference: Rs {detail['saving']:,.0f}</b>",
    ]

    if detail.get("lot_quantity"):
        lines.append("")
        lines.append(
            f"Covers the {detail['lot_quantity']:g} shares bought "
            f"{_escape(detail.get('lot_date', ''))}, not the whole holding."
        )

    lines.extend(["", f"<i>{_escape(_disclaimer(cfg))}</i>"])
    return "\n".join(lines)


def scan_summary(summary: Any, candidates: list[dict[str, Any]], cfg: Any = None) -> str:
    """What the scan found, sent when nothing else would be."""
    cfg = cfg or load_config()

    lines = [
        "<b>Daily scan complete</b>",
        f"{summary.universe_size} scanned in {summary.duration_seconds:.0f}s",
        f"{summary.passed_dip} passed the dip screen, "
        f"{summary.passed_delivery} confirmed on delivery",
        "",
    ]

    if candidates:
        lines.append(f"<b>{len(candidates)} candidate(s)</b>")
        for candidate in candidates[:5]:
            conviction = candidate.get("conviction")
            verdict = f"conviction {conviction:.0f}" if conviction is not None else "not assessed"
            lines.append(
                f"  {_escape(candidate['symbol'])} - Rs {candidate['close']:,.2f}, "
                f"{candidate['drawdown_pct']:.0f}% off high, {verdict}"
            )
    else:
        lines.append("No candidates. Normal near a market high.")

    lines.extend(["", f"<i>{_escape(_disclaimer(cfg))}</i>"])
    return "\n".join(lines)


def failure(message: str, cfg: Any = None) -> str:
    """A scan that did not finish.

    Sent because silence would otherwise be ambiguous: no alert could mean
    "nothing found" or "the job never ran", and those need different responses.
    """
    cfg = cfg or load_config()
    return "\n".join([
        "<b>Daily scan FAILED</b>",
        "",
        _escape(message[:600]),
        "",
        "<i>No candidates were assessed today. This is a job failure, not a quiet market.</i>",
    ])
