"""Backtesting the live BUY rule (option 1, 2026-10-17) with delivery data.

Option 1 buys a stock that passes the screen unless the committee objects.
Of its stages, three can be replayed point-in-time and three cannot:

  replayed    dip rules + trend filter      from Yahoo prices
              delivery confirmation         from NSE's daily bhavcopies, 2020 on
              sizing at conviction 50-59    3% positions, at most 8 (6% heat / 0.75%)
  not         quality gate                  Yahoo keeps ~4 years of statements
              the committee and its veto    past news and filings cannot be rebuilt

So this tests everything mechanical about the rule. The committee is assumed
to pass every candidate, which is close to what option 1 does in practice:
of the first 55 live runs, the only blocks were forensics vetoes.

Delivery confirmation had never been backtested before: NSE's archive of the
full bhavcopy, which carries delivery, starts in 2020.
"""

from __future__ import annotations

from typing import Any, Iterable

import numpy as np
import pandas as pd

from src import indicators as ind
from src.strategy.simulator import Bars


def delivery_frames(bhavcopies: Iterable[pd.DataFrame], symbols: set[str]) -> dict[str, pd.DataFrame]:
    """Per-symbol daily close, delivery % and turnover from many bhavcopies."""
    parts = []
    for day in bhavcopies:
        if day is None or day.empty:
            continue
        series = day.get("SERIES")
        rows = day[series.astype(str).str.strip() == "EQ"] if series is not None else day
        rows = rows[rows["SYMBOL"].isin(symbols)]
        if rows.empty:
            continue
        parts.append(pd.DataFrame({
            "symbol": rows["SYMBOL"].to_numpy(),
            "date": pd.to_datetime(rows["trade_date"]).to_numpy(),
            "close": pd.to_numeric(rows["CLOSE_PRICE"], errors="coerce").to_numpy(),
            "delivery_pct": pd.to_numeric(rows["DELIV_PER"], errors="coerce").to_numpy(),
            "turnover_lacs": pd.to_numeric(rows["TURNOVER_LACS"], errors="coerce").to_numpy(),
        }))
    if not parts:
        return {}
    panel = pd.concat(parts, ignore_index=True)
    out = {}
    for symbol, group in panel.groupby("symbol"):
        frame = group.drop(columns="symbol").set_index("date").sort_index()
        out[symbol] = frame[~frame.index.duplicated(keep="last")]
    return out


def delivery_confirmed(frame: pd.DataFrame, cfg: Any) -> pd.Series:
    """Point-in-time: would screener's delivery stage have passed on each day?

    Mirrors evaluate_delivery: 20-session average delivery at least 40%, the
    mean of the last ten down-day delivery ratios at least 1.05, at least 2 of
    the last 10 sessions down days at or above it, and 20-session average
    turnover at least Rs 5 crore. Every input is backward-looking.
    """
    window = int(cfg.get("delivery.avg_window_days", 20))
    ratio_min = float(cfg.get("delivery.down_day_delivery_ratio_min", 1.05))
    floor = float(cfg.get("delivery.min_delivery_pct", 40.0))
    required = int(cfg.get("delivery.persistence_sessions_required", 2))
    lookback = int(cfg.get("delivery.persistence_lookback", 10))
    turnover_floor = float(cfg.get("delivery.min_avg_traded_value_cr", 5.0))

    close = pd.to_numeric(frame["close"], errors="coerce")
    delivery = pd.to_numeric(frame["delivery_pct"], errors="coerce")
    turnover = pd.to_numeric(frame["turnover_lacs"], errors="coerce")

    average = delivery.rolling(window, min_periods=window).mean()
    ratio = ind.down_day_delivery_ratio(close, delivery, window)
    down = ratio.dropna()
    mean_recent = down.rolling(10, min_periods=1).mean().reindex(ratio.index).ffill()
    persistence = (ratio >= ratio_min).astype(int).rolling(lookback, min_periods=1).sum()
    liquid = turnover.rolling(window, min_periods=window).mean() / 100.0

    ok = (average >= floor) & (mean_recent >= ratio_min) & (persistence >= required) & (liquid >= turnover_floor)
    return ok.fillna(False).astype(bool)


def confirm(signals: dict[str, np.ndarray], bars: dict[str, Bars],
            deliveries: dict[str, pd.DataFrame], cfg: Any) -> dict[str, np.ndarray]:
    """Dip signals that delivery also confirmed on the same day.

    A day with no bhavcopy row for the stock (before 2020, or a missing file)
    is not confirmed: no data is never a pass, as in the live screen.
    """
    out = {}
    for symbol, signal in signals.items():
        frame = deliveries.get(symbol)
        if frame is None or frame.empty:
            out[symbol] = np.zeros_like(signal)
            continue
        mask = delivery_confirmed(frame, cfg)
        aligned = mask.reindex(pd.DatetimeIndex(bars[symbol].dates), fill_value=False).to_numpy(dtype=bool)
        out[symbol] = signal & aligned
    return out


def entries_per_year(rows: list[dict[str, Any]]) -> dict[int, int]:
    counts: dict[int, int] = {}
    for row in rows:
        when = row.get("entry_date")
        if when is not None:
            counts[when.year] = counts.get(when.year, 0) + 1
    return dict(sorted(counts.items()))
