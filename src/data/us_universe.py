"""The US universe: S&P 500 and Nasdaq-100 constituents, plus a hand-picked
list of ETFs from config.

The index lists come from Wikipedia's constituent tables, which are kept
current within days of each change and are the usual free source. They are
today's members only, so any backtest over them carries survivorship bias:
companies that fell out of the S&P 500 are missing, and in the US that bias
is large, because the index adds winners as they grow.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from src.data.cache import cached_fetch, make_key
from src.data.provider import DataResult, DataStatus, StockIdentity

log = logging.getLogger(__name__)

SOURCE = "wikipedia"

PAGES = {
    "sp500": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
    "nasdaq100": "https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies",
}

#: Wikipedia refuses requests without a descriptive User-Agent.
USER_AGENT = "dip-committee/1.0 (research; https://github.com/neevoswal19-maker/dip-committee)"

#: Fewer rows than this means the page changed shape, not the index.
MIN_ROWS = {"sp500": 450, "nasdaq100": 90}


def _headers(table) -> list[str]:
    first = table.find("tr")
    cells = first.find_all(["th", "td"]) if first else []
    # "Symbol[1]" -> "Symbol": footnote markers are not part of the name.
    return [re.sub(r"\[.*?\]", "", c.get_text(" ", strip=True)).strip() for c in cells]


def _constituent_table(soup):
    """id="constituents" if present, else the first table with a ticker column."""
    table = soup.find("table", id="constituents")
    if table is not None:
        return table
    for candidate in soup.find_all("table"):
        if {"Symbol", "Ticker"} & set(_headers(candidate)):
            return candidate
    return None


def parse_constituents(html: str) -> list[StockIdentity]:
    """Ticker, name and GICS sector from a Wikipedia constituents table.

    Parsed with BeautifulSoup's built-in parser rather than pandas.read_html,
    which falls back to html5lib (not installed) on Wikipedia's markup.
    """
    from bs4 import BeautifulSoup

    table = _constituent_table(BeautifulSoup(html, "html.parser"))
    if table is None:
        return []
    headers = _headers(table)
    position = {name: i for i, name in enumerate(headers)}
    symbol_i = next((position[c] for c in ("Symbol", "Ticker") if c in position), None)
    if symbol_i is None:
        return []
    name_i = next((position[c] for c in ("Security", "Company") if c in position), None)
    # The Nasdaq-100 page classifies by ICB, the S&P 500 page by GICS.
    sector_i = next((position[c] for c in ("GICS Sector", "ICB Industry") if c in position), None)
    industry_i = next((position[c] for c in ("GICS Sub-Industry", "ICB Subsector") if c in position), None)

    def cell(cells, i):
        return None if i is None or i >= len(cells) else cells[i] or None

    out = []
    for row in table.find_all("tr")[1:]:
        cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
        symbol = (cell(cells, symbol_i) or "").strip().upper()
        if not symbol:
            continue
        out.append(StockIdentity(symbol=symbol, name=cell(cells, name_i), sector=cell(cells, sector_i),
                                 industry=cell(cells, industry_i), market="US"))
    return out


def _fetch_page(url: str) -> str:
    import requests

    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    response.raise_for_status()
    return response.text


def get_index(which: str) -> DataResult[list[StockIdentity]]:
    if which not in PAGES:
        raise ValueError(f"unknown US index {which!r}")

    html = cached_fetch("universe", make_key("us", which), lambda: _fetch_page(PAGES[which]),
                        source=SOURCE)
    if not html:
        return DataResult.unavailable(SOURCE, f"could not load the {which} list")
    members = parse_constituents(html)
    if len(members) < MIN_ROWS[which]:
        return DataResult.unavailable(SOURCE, f"{which} list has only {len(members)} rows")
    return DataResult(value=members, status=DataStatus.OK, source=SOURCE)


def stocks() -> list[StockIdentity]:
    """S&P 500 and Nasdaq-100, without duplicates; S&P 500 details win."""
    seen: dict[str, StockIdentity] = {}
    for which in ("sp500", "nasdaq100"):
        result = get_index(which)
        if not result.value:
            log.warning("US universe: %s", result.note)
            continue
        for stock in result.value:
            seen.setdefault(stock.symbol, stock)
    return list(seen.values())


def etfs(cfg: Any) -> list[StockIdentity]:
    return [StockIdentity(symbol=str(s).upper(), sector="ETF", market="US")
            for s in (cfg.get("us.etfs", []) or [])]
