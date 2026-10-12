"""Symbols with "&" in them must reach NSE whole.

Left raw, `symbol=J&KBANK` is read as `symbol=J` plus a stray `KBANK`
parameter. NSE answers with nothing, and the filings, insider and
shareholding bots went blind on J&KBANK without any error being raised.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from src.data import nse


@pytest.fixture
def requested(monkeypatch):
    urls: list[str] = []

    def fake_fetch_json(url, *, referer=None):
        urls.append(url)
        return {"data": []}

    monkeypatch.setattr(nse, "_fetch_json", fake_fetch_json)
    monkeypatch.setattr(nse, "cached_fetch", lambda namespace, key, fetcher, **kw: fetcher())
    return urls


@pytest.mark.parametrize("fetch", [nse.get_insider_trades, nse.get_shareholding_pattern,
                                   nse.get_corporate_announcements])
@pytest.mark.parametrize("symbol", ["J&KBANK", "M&M", "INFY"])
def test_the_symbol_arrives_intact(requested, fetch, symbol):
    fetch(symbol)
    query = parse_qs(urlparse(requested[-1]).query)
    assert query["symbol"] == [symbol]
    assert "KBANK" not in query


def test_an_ordinary_symbol_keeps_its_cache_key():
    """Only "&" symbols move to a new key, so the rest of the cache stays warm."""
    assert nse._symbol_param("INFY") == "INFY"
    assert nse._symbol_param("J&KBANK") == "J%26KBANK"
