"""Price, index and macro history from Yahoo Finance.

Yahoo is used for OHLCV rather than NSE because it serves years of history in
one request, survives NSE's blocking, and needs no cookie dance. What it does
not carry is deliverable quantity - that is NSE-only, and lives in nse.py.

Everything here returns lowercase columns on an ascending DatetimeIndex, so
downstream code never has to guess whether it is holding 'Close' or 'close'.
"""

from __future__ import annotations

import logging
import warnings
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

from src.data.cache import cached_fetch, get_cache, get_limiter, make_key, ttl_for
from src.data.provider import DataResult, DataStatus, StockIdentity

log = logging.getLogger(__name__)

# yfinance is chatty about delisted tickers and future pandas behaviour.
warnings.filterwarnings("ignore", category=FutureWarning, module="yfinance")

SOURCE = "yahoo"

# Yahoo symbols for the macro series the geopolitical desk reads.
MACRO_SYMBOLS = {
    "nifty50": "^NSEI",
    "nifty500": "^CRSLDX",
    "india_vix": "^INDIAVIX",
    "usdinr": "INR=X",
    "crude_brent": "BZ=F",
    "gold": "GC=F",
    "us_10y": "^TNX",
}

# NSE sector indices, for the Industry Research Analyst's relative strength.
SECTOR_INDEX_SYMBOLS = {
    "Financial Services": "^CNXFIN",
    "Banking": "^NSEBANK",
    "Information Technology": "^CNXIT",
    "Automobile": "^CNXAUTO",
    "Pharmaceuticals": "^CNXPHARMA",
    "FMCG": "^CNXFMCG",
    "Metals": "^CNXMETAL",
    "Energy": "^CNXENERGY",
    "Realty": "^CNXREALTY",
    "Media": "^CNXMEDIA",
    "PSU Bank": "^CNXPSUBANK",
    "Infrastructure": "^CNXINFRA",
}


def _normalise_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """Lowercase columns, drop timezone, sort ascending, drop empty rows."""
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()

    # A single-ticker download can still come back multi-indexed.
    if isinstance(out.columns, pd.MultiIndex):
        out.columns = out.columns.get_level_values(0)

    out.columns = [str(c).strip().lower().replace(" ", "_") for c in out.columns]

    if "adj_close" in out.columns and "close" not in out.columns:
        out = out.rename(columns={"adj_close": "close"})

    if not isinstance(out.index, pd.DatetimeIndex):
        out.index = pd.to_datetime(out.index, errors="coerce")
    if getattr(out.index, "tz", None) is not None:
        out.index = out.index.tz_localize(None)

    out.index.name = "date"
    out = out[~out.index.isna()]
    out = out.sort_index()
    out = out[~out.index.duplicated(keep="last")]

    # A row with no close is worthless and would poison every rolling window.
    if "close" in out.columns:
        out = out[out["close"].notna()]

    keep = [c for c in ["open", "high", "low", "close", "adj_close", "volume"] if c in out.columns]
    return out[keep]


def _download(symbol: str, start: date, end: date) -> pd.DataFrame:
    import yfinance as yf

    raw = yf.download(
        symbol,
        start=start.isoformat(),
        end=(end + timedelta(days=1)).isoformat(),
        interval="1d",
        progress=False,
        auto_adjust=False,
        actions=False,
        threads=False,
        multi_level_index=False,
    )
    return _normalise_ohlcv(raw)


def get_price_history(
    stock: StockIdentity | str,
    years: float = 6.0,
    *,
    end: date | None = None,
    force_refresh: bool = False,
) -> DataResult[pd.DataFrame]:
    """Daily OHLCV for an NSE-listed stock."""
    identity = StockIdentity(stock) if isinstance(stock, str) else stock
    end_date = end or date.today()
    start_date = end_date - timedelta(days=int(years * 365.25) + 10)

    key = make_key("prices", identity.yahoo, start_date, end_date)
    df = cached_fetch(
        "prices",
        key,
        lambda: _download(identity.yahoo, start_date, end_date),
        source="yahoo",
        force_refresh=force_refresh,
    )

    if df is None:
        return DataResult.unavailable(SOURCE, f"price download failed for {identity.yahoo}")
    if df.empty:
        return DataResult.empty(SOURCE, f"no price rows returned for {identity.yahoo}")

    last_bar = df.index[-1].date()
    # Weekends and holidays make a one-day gap normal; a week is not.
    status = DataStatus.OK if (end_date - last_bar).days <= 5 else DataStatus.STALE
    note = None if status is DataStatus.OK else f"last bar {last_bar.isoformat()}"

    return DataResult(value=df, status=status, source=SOURCE, as_of=last_bar, note=note)


def get_price_history_batch(
    stocks: list[StockIdentity],
    years: float = 6.0,
    *,
    chunk_size: int = 40,
    end: date | None = None,
) -> dict[str, DataResult[pd.DataFrame]]:
    """Fetch many symbols per request instead of one at a time.

    Screening the Nifty 500 one symbol per call takes roughly 35 minutes,
    almost all of it network round-trips. Yahoo accepts space-separated
    ticker lists, so batching cuts the same scan to a couple of minutes -
    which is the difference between a daily job that fits comfortably in a
    scheduled run and one that does not.

    Already-cached symbols are served from disk and excluded from the
    request, so a re-run costs nothing.
    """
    import yfinance as yf

    end_date = end or date.today()
    start_date = end_date - timedelta(days=int(years * 365.25) + 10)
    cache = get_cache()
    ttl = ttl_for("prices")

    results: dict[str, DataResult[pd.DataFrame]] = {}
    pending: list[StockIdentity] = []

    for stock in stocks:
        key = make_key("prices", stock.yahoo, start_date, end_date)
        hit = cache.get("prices", key, ttl)
        if hit is not None and not hit.empty:
            results[stock.symbol] = DataResult(
                value=hit, status=DataStatus.OK, source=SOURCE, as_of=hit.index[-1].date()
            )
        else:
            pending.append(stock)

    log.info("Batch prices: %d cached, %d to fetch", len(results), len(pending))

    for offset in range(0, len(pending), chunk_size):
        chunk = pending[offset : offset + chunk_size]
        tickers = " ".join(s.yahoo for s in chunk)

        try:
            get_limiter("yahoo").acquire()
            raw = yf.download(
                tickers,
                start=start_date.isoformat(),
                end=(end_date + timedelta(days=1)).isoformat(),
                interval="1d",
                progress=False,
                auto_adjust=False,
                actions=False,
                threads=True,
                group_by="ticker",
            )
        except Exception as exc:
            log.warning("Batch download failed for %d symbols: %s", len(chunk), exc)
            for stock in chunk:
                results[stock.symbol] = DataResult.unavailable(SOURCE, f"batch fetch failed: {exc}")
            continue

        for stock in chunk:
            try:
                # A one-ticker chunk comes back flat rather than grouped.
                if isinstance(raw.columns, pd.MultiIndex):
                    if stock.yahoo not in raw.columns.get_level_values(0):
                        results[stock.symbol] = DataResult.unavailable(SOURCE, "symbol absent from batch response")
                        continue
                    frame = raw[stock.yahoo]
                else:
                    frame = raw

                df = _normalise_ohlcv(frame)
                if df.empty:
                    results[stock.symbol] = DataResult.empty(SOURCE, "no rows returned")
                    continue

                key = make_key("prices", stock.yahoo, start_date, end_date)
                cache.set("prices", key, df)

                last_bar = df.index[-1].date()
                status = DataStatus.OK if (end_date - last_bar).days <= 5 else DataStatus.STALE
                results[stock.symbol] = DataResult(
                    value=df, status=status, source=SOURCE, as_of=last_bar
                )
            except Exception as exc:
                log.debug("Could not extract %s from batch: %s", stock.symbol, exc)
                results[stock.symbol] = DataResult.unavailable(SOURCE, f"extract failed: {exc}")

    return results


def get_index_history(
    index_symbol: str,
    years: float = 2.0,
    *,
    force_refresh: bool = False,
) -> DataResult[pd.DataFrame]:
    """History for an index by Yahoo symbol, or by the sector names above."""
    symbol = SECTOR_INDEX_SYMBOLS.get(index_symbol, MACRO_SYMBOLS.get(index_symbol, index_symbol))

    end_date = date.today()
    start_date = end_date - timedelta(days=int(years * 365.25) + 10)

    key = make_key("index", symbol, start_date, end_date)
    df = cached_fetch(
        "prices",
        key,
        lambda: _download(symbol, start_date, end_date),
        source="yahoo",
        force_refresh=force_refresh,
    )

    if df is None or df.empty:
        return DataResult.unavailable(SOURCE, f"no index data for {symbol}")

    return DataResult(value=df, status=DataStatus.OK, source=SOURCE, as_of=df.index[-1].date())


def get_macro_series(*, years: float = 2.0) -> DataResult[dict[str, pd.DataFrame]]:
    """Crude, USDINR, VIX and the rest, for the geopolitical desk.

    Partial success is the normal case - Yahoo's coverage of India VIX in
    particular comes and goes - so missing members are reported rather than
    treated as a failure.
    """
    series: dict[str, pd.DataFrame] = {}
    missing: list[str] = []

    for name, symbol in MACRO_SYMBOLS.items():
        result = get_index_history(symbol, years=years)
        if result.usable and result.value is not None and not result.value.empty:
            series[name] = result.value
        else:
            missing.append(name)

    if not series:
        return DataResult.unavailable(SOURCE, "no macro series could be fetched")

    status = DataStatus.OK if not missing else DataStatus.PARTIAL
    note = None if not missing else f"missing: {', '.join(missing)}"
    return DataResult(value=series, status=status, source=SOURCE, as_of=date.today(), note=note)


def get_quote_snapshot(stock: StockIdentity | str) -> DataResult[dict[str, Any]]:
    """Latest price and the handful of fields the dashboard shows at a glance."""
    identity = StockIdentity(stock) if isinstance(stock, str) else stock

    def fetch() -> dict[str, Any]:
        import yfinance as yf

        info = yf.Ticker(identity.yahoo).fast_info
        return {
            "last_price": getattr(info, "last_price", None),
            "previous_close": getattr(info, "previous_close", None),
            "day_high": getattr(info, "day_high", None),
            "day_low": getattr(info, "day_low", None),
            "year_high": getattr(info, "year_high", None),
            "year_low": getattr(info, "year_low", None),
            "market_cap": getattr(info, "market_cap", None),
            "currency": getattr(info, "currency", "INR"),
        }

    key = make_key("quote", identity.yahoo, date.today())
    data = cached_fetch("prices", key, fetch, ttl_hours=1.0, source="yahoo")

    if data is None or data.get("last_price") is None:
        return DataResult.unavailable(SOURCE, f"no quote for {identity.yahoo}")
    return DataResult(value=data, status=DataStatus.OK, source=SOURCE, as_of=date.today())


def latest_close(history: pd.DataFrame) -> float | None:
    if history is None or history.empty or "close" not in history.columns:
        return None
    return float(history["close"].iloc[-1])


def trading_days_between(history: pd.DataFrame, start: datetime, end: datetime) -> int:
    """Sessions in a window, counted from actual bars rather than a calendar.

    NSE holidays are irregular enough that counting real bars is the only
    reliable way to measure a holding period in sessions.
    """
    if history is None or history.empty:
        return 0
    mask = (history.index >= pd.Timestamp(start)) & (history.index <= pd.Timestamp(end))
    return int(mask.sum())


# --- The price at a moment -------------------------------------------------------


def _minute_bars(yahoo_symbol: str) -> pd.DataFrame | None:
    """The last seven days of one-minute bars, the most Yahoo keeps."""
    import yfinance as yf

    frame = yf.Ticker(yahoo_symbol).history(period="7d", interval="1m", auto_adjust=False)
    return frame if frame is not None and not frame.empty else None


def _latest_quote(yahoo_symbol: str) -> float | None:
    import yfinance as yf

    value = getattr(yf.Ticker(yahoo_symbol).fast_info, "last_price", None)
    return float(value) if value else None


def price_at(stock: StockIdentity | str, when: datetime) -> tuple[float, str] | None:
    """The last traded price at or before `when` (IST, naive), and how it was found.

    Used to price a purchase reported by message, where the owner gives the
    quantity but not the fill. The minute bar at the moment the message was
    sent is the closest estimate available after the fact; a message sent in
    the evening gets that day's last trade, a weekend message Friday's.
    Falls back to the live quote, uncached, when no minute bars come back.
    Returns None when neither source answers - the caller must then ask for
    the price rather than invent one.
    """
    identity = StockIdentity(stock) if isinstance(stock, str) else stock
    when_utc = pd.Timestamp(when - timedelta(hours=5, minutes=30), tz="UTC")

    try:
        bars = _minute_bars(identity.yahoo)
    except Exception as exc:
        log.info("Minute bars unavailable for %s: %s", identity.yahoo, exc)
        bars = None

    if bars is not None:
        index = bars.index
        index = index.tz_localize("UTC") if index.tz is None else index.tz_convert("UTC")
        closes = pd.Series(bars["Close"].to_numpy(), index=index).dropna()
        closes = closes[closes.index <= when_utc]
        if not closes.empty and (when_utc - closes.index[-1]) <= pd.Timedelta(days=4):
            stamp = (closes.index[-1] + pd.Timedelta(hours=5, minutes=30)).tz_localize(None)
            return round(float(closes.iloc[-1]), 2), f"last trade at {stamp:%d %b %H:%M}"

    try:
        quote = _latest_quote(identity.yahoo)
    except Exception as exc:
        log.info("Quote unavailable for %s: %s", identity.yahoo, exc)
        quote = None
    if quote:
        return round(quote, 2), "latest quote"
    return None
