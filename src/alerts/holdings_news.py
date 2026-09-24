"""A morning news check on everything held.

The committee reads news only for the day's top screen candidates. Holdings
were watched on price and tax rules alone, so a fraud headline on a stock
already owned would go unnoticed until the price reached the stop. This
closes that gap with the news desk's own event classifier
(`agents.lexicon.classify_event`), so a holding is judged by the same rules
as a candidate.

Only serious negative events alert: governance (fraud, regulator orders, an
auditor resigning), promoter pledges, litigation, operational shocks, and
rating downgrades. Routine filings - most of what NSE publishes - are
classified as routine and ignored. Each item alerts once, ever: the dedupe key
is a hash of its text.

The classifier works on keywords, not meaning. The alert says so, and asks
for the item to be read before acting on it.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from src import db
from src.agents import lexicon

log = logging.getLogger(__name__)

#: Filings and headlines newer than this are checked. Four days covers a
#: weekend plus a holiday; older items were seen on an earlier morning.
LOOKBACK_DAYS = 4

SERIOUS_EVENTS = {
    "governance": "Governance",
    "pledge": "Promoter pledge",
    "litigation": "Legal",
    "operational": "Operational",
    "rating_downgrade": "Rating downgrade",
}


@dataclass
class NewsFlag:
    symbol: str
    event: str            # a key of SERIOUS_EVENTS
    text: str
    origin: str           # "NSE filing" or the publisher
    when: datetime | None
    link: str | None = None
    held: float = 0.0

    @property
    def dedupe_key(self) -> str:
        normalised = re.sub(r"\s+", " ", self.text.lower()).strip()
        digest = hashlib.sha1(f"{self.symbol}|{normalised}".encode()).hexdigest()[:16]
        return f"holding_news|{self.symbol}|{digest}"


def serious_event(text: str) -> str | None:
    """The serious negative event a filing or headline describes, if any."""
    name, _ = lexicon.classify_event(text)
    if name in SERIOUS_EVENTS:
        return name
    if name == "rating_action" and re.search(r"downgrad", text.lower()):
        return "rating_downgrade"
    return None


def _filings(symbol: str, since: datetime) -> list[NewsFlag]:
    from src.agents.news_desk import _announcement_date, _announcement_text
    from src.data import nse

    result = nse.get_corporate_announcements(symbol, days=LOOKBACK_DAYS)
    frame = result.value
    if frame is None or getattr(frame, "empty", True):
        return []

    flags = []
    for _, row in frame.iterrows():
        text = _announcement_text(row)
        when = _announcement_date(row)
        if not text or (when is not None and when < since):
            continue
        event = serious_event(text)
        if event:
            flags.append(NewsFlag(symbol, event, text.strip()[:300], "NSE filing", when))
    return flags


def _headlines(symbol: str, name: str | None, since: datetime) -> list[NewsFlag]:
    from src.data import news
    from src.data.provider import StockIdentity

    result = news.get_news(StockIdentity(symbol, name=name), days=LOOKBACK_DAYS)
    flags = []
    for item in result.value or []:
        text = item.get("title") or ""
        when = item.get("published")
        if not text or (when is not None and when < since):
            continue
        event = serious_event(text)
        if event:
            flags.append(NewsFlag(symbol, event, text.strip()[:300],
                                  item.get("source") or "news", when, item.get("link")))
    return flags


def find(cfg: Any = None) -> tuple[list[NewsFlag], list[str]]:
    """Serious news on every open position. Returns (flags, symbols not checked).

    A source failing for one holding is reported, not swallowed: "no bad news"
    and "could not look" must not read the same in the morning summary.
    """
    from sqlalchemy import select

    from src import portfolio as pf

    since = db.now() - timedelta(days=LOOKBACK_DAYS)
    flags: list[NewsFlag] = []
    unchecked: list[str] = []

    for state in pf.open_positions(cfg=cfg):
        with db.connection() as conn:
            row = conn.execute(
                select(db.stocks.c.name).where(db.stocks.c.symbol == state.symbol)
            ).first()
        name = row.name if row else None

        found: list[NewsFlag] = []
        failures = 0
        for source in (lambda: _filings(state.symbol, since),
                       lambda: _headlines(state.symbol, name, since)):
            try:
                found.extend(source())
            except Exception as exc:
                failures += 1
                log.warning("News check for %s partly failed: %s", state.symbol, exc)
        if failures == 2:
            unchecked.append(state.symbol)

        # The same event often arrives as a filing and a headline; keep one
        # per distinct text.
        seen: set[str] = set()
        for flag in found:
            flag.held = state.quantity
            if flag.dedupe_key not in seen:
                seen.add(flag.dedupe_key)
                flags.append(flag)

    return flags, unchecked


def message(flag: NewsFlag) -> str:
    from src.alerts.telegram import _escape

    when = f" · {flag.when:%d %b %H:%M}" if flag.when else ""
    lines = [
        f"<b>News on {_escape(flag.symbol)}</b> (you hold {flag.held:g})",
        f"<b>{SERIOUS_EVENTS[flag.event]}:</b> {_escape(flag.text)}",
        f"{_escape(flag.origin)}{when}",
    ]
    if flag.link:
        lines.append(_escape(flag.link))
    lines.extend([
        "",
        "Flagged by keyword rules, which can't judge how serious this really is. "
        "Read it before acting. Your price-based exit rules haven't changed.",
    ])
    return "\n".join(lines)


def send(flags: list[NewsFlag], cfg: Any = None) -> int:
    """Alert on each flag not alerted before. Returns how many went out."""
    from src.alerts import telegram

    sent = 0
    for flag in flags:
        if telegram.send(message(flag), alert_type="holding_news", symbol=flag.symbol,
                         dedupe_key=flag.dedupe_key, cfg=cfg):
            sent += 1
    return sent
