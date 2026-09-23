"""Headlines from public RSS feeds.

Headlines only, deliberately. Full article text would need scraping a dozen
paywalled sites, and the news desk's lexicon works on headlines anyway - a
body it cannot read well adds cost without adding signal.

Google News RSS carries the source domain in the item, which is what the News
Credibility Analyst tiers on, and searching by company name there reaches far
more outlets than any single publisher's feed.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import quote_plus

from src.data.cache import cached_fetch, make_key
from src.data.provider import DataResult, DataStatus, StockIdentity

log = logging.getLogger(__name__)

SOURCE = "rss"

GOOGLE_NEWS = "https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"

#: Publisher feeds, as a supplement rather than the main source.
PUBLISHER_FEEDS = {
    "economictimes.indiatimes.com": "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms",
    "moneycontrol.com": "https://www.moneycontrol.com/rss/marketreports.xml",
    "business-standard.com": "https://www.business-standard.com/rss/markets-106.rss",
}


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", str(text or ""))).strip()


def _source_from_entry(entry: Any, link: str) -> str:
    """Google News wraps the publisher in `source`; otherwise take the domain."""
    source = getattr(entry, "source", None)
    if source is not None:
        title = getattr(source, "title", None) or (source.get("title") if isinstance(source, dict) else None)
        href = getattr(source, "href", None) or (source.get("href") if isinstance(source, dict) else None)
        if href:
            return _domain(href)
        if title:
            return str(title)

    # Google News titles end with " - Publisher".
    title = _clean(getattr(entry, "title", ""))
    if " - " in title:
        return title.rsplit(" - ", 1)[-1]

    return _domain(link)


def _domain(url: str) -> str:
    match = re.search(r"https?://([^/]+)", str(url or ""))
    return match.group(1).replace("www.", "") if match else ""


def _published(entry: Any) -> datetime | None:
    for attribute in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, attribute, None)
        if parsed:
            try:
                return datetime(*parsed[:6])
            except (TypeError, ValueError):
                continue
    return None


def _fetch_feed(url: str, limit: int) -> list[dict[str, Any]]:
    import feedparser

    parsed = feedparser.parse(url)
    items: list[dict[str, Any]] = []

    for entry in parsed.entries[:limit]:
        link = getattr(entry, "link", "")
        title = _clean(getattr(entry, "title", ""))
        if not title:
            continue

        source = _source_from_entry(entry, link)
        # Strip the trailing " - Publisher" once the source is extracted.
        if source and title.endswith(f" - {source}"):
            title = title[: -(len(source) + 3)].strip()

        items.append({
            "title": title,
            "summary": _clean(getattr(entry, "summary", ""))[:400],
            "link": link,
            "source": source,
            "published": _published(entry),
        })

    return items


def get_news(
    stock: StockIdentity | str,
    days: int = 30,
    *,
    limit: int = 40,
    company_name: str | None = None,
) -> DataResult[list[dict[str, Any]]]:
    """Recent headlines for one company.

    Searches by company name where known, because a bare NSE symbol matches
    poorly - "INFY" returns far less than "Infosys".
    """
    identity = StockIdentity(stock.upper()) if isinstance(stock, str) else stock
    name = company_name or identity.name or identity.symbol

    # Quoting the name and adding an NSE qualifier cuts most false matches.
    query = quote_plus(f'"{name}" stock NSE')

    def fetch() -> list[dict[str, Any]]:
        return _fetch_feed(GOOGLE_NEWS.format(query=query), limit)

    items = cached_fetch(
        "news", make_key("news", identity.symbol, days), fetch, source="rss"
    )

    if items is None:
        return DataResult.unavailable(SOURCE, f"news feed unreachable for {identity.symbol}")
    if not items:
        return DataResult.empty(SOURCE, f"no headlines found for {name}")

    cutoff = datetime.now() - timedelta(days=days)
    recent = [
        item for item in items
        if item.get("published") is None or item["published"] >= cutoff
    ]

    if not recent:
        return DataResult.empty(SOURCE, f"no headlines in the last {days} days")

    dated = [i["published"] for i in recent if i.get("published")]
    as_of = max(dated).date() if dated else date.today()

    # Undated items are kept but flagged: the Credibility Analyst treats an
    # item with no timestamp as weaker evidence.
    undated = len(recent) - len(dated)
    note = f"{undated} of {len(recent)} items carry no timestamp" if undated else None

    return DataResult(
        value=recent,
        status=DataStatus.OK if not undated else DataStatus.PARTIAL,
        source=SOURCE,
        as_of=as_of,
        note=note,
    )
