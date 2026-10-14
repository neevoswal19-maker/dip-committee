"""The US universe: S&P 500 and Nasdaq-100 constituents, plus a hand-picked
list of ETFs from config.

The index lists come from Wikipedia's constituent tables, which are kept
current within days of each change and are the usual free source. They are
today's members only, so any backtest over them carries survivorship bias:
companies that fell out of the S&P 500 are missing, and in the US that bias
is large, because the index adds winners as they grow.
"""

from __future__ import annotations

import io
import logging
from typing import Any

import pandas as pd

from src.data.cache import cached_fetch, make_key
from src.data.provider import DataResult, DataStatus, StockIdentity

log = logging.getLogger(__name__)

SOURCE = "wikipedia"

PAGES = {
    "sp500": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
    "nasdaq100": "https://en.wikipedia.org/wiki/Nasdaq-100",
}

#: Wikipedia refuses requests without a descriptive User-Agent.
USER_AGENT = "dip-committee/1.0 (research; https://github.com/neevoswal19-maker/dip-committee)"

#: Fewer rows than this means the page changed shape, not the index.
MIN_ROWS = {"sp500": 450, "nasdaq100": 90}


def parse_constituents(html: str) -> list[StockIdentity]:
    """The table with id="constituents": ticker, name and GICS sector."""
    tables = pd.read_html(io.StringIO(html), attrs={"id": "constituents"})
    if not tables:
        return []
    table = tables[0]
    columns = {str(c).strip(): c for c in table.columns}
    symbol_col = next((columns[c] for c in ("Symbol", "Ticker") if c in columns), None)
    if symbol_col is None:
        return []
    name_col = next((columns[c] for c in ("Security", "Company") if c in columns), None)
    sector_col = columns.get("GICS Sector")
    industry_col = columns.get("GICS Sub-Industry")

    out = []
    for _, row in table.iterrows():
        symbol = str(row[symbol_col]).strip().upper()
        if not symbol or symbol == "NAN":
            continue
        out.append(StockIdentity(
            symbol=symbol,
            name=None if name_col is None else str(row[name_col]).strip(),
            sector=None if sector_col is None else str(row[sector_col]).strip(),
            industry=None if industry_col is None else str(row[industry_col]).strip(),
            market="US",
        ))
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
