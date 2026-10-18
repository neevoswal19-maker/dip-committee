"""5-minute bars for the intraday strategy: from Yahoo, kept in the database.

Yahoo serves the last 60 sessions of 5-minute bars, with volume, for NSE ETFs
(the Nifty and Bank Nifty indices carry no volume, so VWAP needs the ETFs).
Every bar fetched is stored in `intraday_bars`, so the history grows past that
window and the rules can later be backtested on more than 60 sessions.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import select

from src import db

log = logging.getLogger(__name__)

COLUMNS = ["open", "high", "low", "close", "volume"]


def symbols(cfg: Any) -> list[str]:
    return list(((cfg.get("intraday", {}) or {}).get("symbols", {}) or {}).keys())


def _to_ist(index: pd.Index) -> pd.DatetimeIndex:
    index = pd.DatetimeIndex(index)
    if index.tz is not None:
        index = index.tz_convert("Asia/Kolkata").tz_localize(None)
    return index


def fetch(names: list[str], period: str = "60d") -> dict[str, pd.DataFrame]:
    """5-minute bars from Yahoo, indexed by bar start in IST (naive)."""
    import yfinance as yf

    tickers = [f"{s}.NS" for s in names]
    raw = yf.download(" ".join(tickers), period=period, interval="5m", auto_adjust=False,
                      progress=False, group_by="ticker", threads=True)
    out = {}
    for name, ticker in zip(names, tickers):
        try:
            frame = raw[ticker] if isinstance(raw.columns, pd.MultiIndex) else raw
        except KeyError:
            log.warning("No 5-minute bars for %s", ticker)
            continue
        frame = frame.rename(columns=lambda c: str(c).strip().lower())
        frame = frame[[c for c in COLUMNS if c in frame.columns]].dropna(subset=["close"])
        frame.index = _to_ist(frame.index)
        out[name] = frame[~frame.index.duplicated(keep="last")].sort_index()
    return out


def completed(frame: pd.DataFrame, now: datetime, minutes: int = 5) -> pd.DataFrame:
    """Only bars that had closed by `now` (IST, naive): Yahoo includes the one still forming."""
    if frame is None or frame.empty:
        return frame
    return frame[frame.index + pd.Timedelta(minutes=minutes) <= pd.Timestamp(now)]


def store(frames: dict[str, pd.DataFrame]) -> int:
    """Insert bars not already stored. Settled bars never change."""
    written = 0
    for name, frame in frames.items():
        if frame is None or frame.empty:
            continue
        with db.connection() as conn:
            existing = {row.ts for row in conn.execute(
                select(db.intraday_bars.c.ts).where(db.intraday_bars.c.symbol == name)
                .where(db.intraday_bars.c.ts >= frame.index.min().to_pydatetime()))}
            rows = [{"symbol": name, "ts": ts.to_pydatetime(), **{c: float(r[c]) for c in COLUMNS if c in r}}
                    for ts, r in frame.iterrows() if ts.to_pydatetime() not in existing]
            # One multi-row INSERT per thousand bars: row-by-row inserts cost a
            # round trip each to a database on another continent.
            for start in range(0, len(rows), 1000):
                conn.execute(db.intraday_bars.insert().values(rows[start:start + 1000]))
            written += len(rows)
    return written


def load(names: list[str], since: datetime | None = None) -> dict[str, pd.DataFrame]:
    out = {}
    with db.connection() as conn:
        for name in names:
            query = select(db.intraday_bars).where(db.intraday_bars.c.symbol == name)
            if since is not None:
                query = query.where(db.intraday_bars.c.ts >= since)
            rows = conn.execute(query.order_by(db.intraday_bars.c.ts)).fetchall()
            if rows:
                frame = pd.DataFrame([{c: getattr(r, c) for c in COLUMNS} for r in rows],
                                     index=pd.DatetimeIndex([r.ts for r in rows]))
                out[name] = frame
    return out


def merge(stored: dict[str, pd.DataFrame], fresh: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Stored history plus fresh bars; the fresh copy wins where both exist."""
    out = {}
    for name in set(stored) | set(fresh):
        parts = [f for f in (stored.get(name), fresh.get(name)) if f is not None and not f.empty]
        if parts:
            frame = pd.concat(parts)
            out[name] = frame[~frame.index.duplicated(keep="last")].sort_index()
    return out


def recent(frame: pd.DataFrame, days: int = 6) -> pd.DataFrame:
    """The last few sessions: enough to warm the 20 EMA and RSI up."""
    if frame is None or frame.empty:
        return frame
    return frame[frame.index >= frame.index.max() - timedelta(days=days)]
