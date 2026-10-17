"""ETF relative-strength swing systems (2A metals, 2B sectors, 3 the portfolio).

Rules as specified by the owner on 2026-10-17, applied on daily bars:

Entry (2A and 2B)
  - close above a rising 50-day SMA (above its value 5 sessions ago)
  - 20-day EMA above the 50-day SMA, and rising
  - RS ratio (ETF close / Nifty close) above its own 20-day SMA
  - not extended: close no more than 2 x ATR(14) above the 20-day EMA
  - a 3-7 session pullback: the highest high of the last 8 sessions came at
    least 3 sessions ago, and some low in the last 7 came within 1% of the
    20-day EMA, on volume below its 20-day average (skipped when the series
    has no volume, as with index proxies)
  - trigger: close above the 20-day EMA and above the previous day's high
  - fill at the next session's open
2B adds: at least 3 of the sector's 5 largest stocks above their own 50-day
SMA, and no new entry while the Nifty is below its 50-day SMA.

Exit
  - stop 1.5 x ATR(14) below entry (1R)
  - at +2R (entry + 3 x ATR): 2A switches to trailing; 2B sells 40% there and
    trails the rest. Trailing exit: a close more than 0.5% below the 20-day
    EMA, sold at the next open. The hard stop is dropped once trailing.
  - 2B sector positions are sold at the next open when the Nifty closes below
    its 50-day SMA (the regime switch).

Portfolio (3): Rs 5 lakh, Rs 5,000 (1%) risked per position at the stop, at
most Rs 1.4 lakh in one position, at most 3 positions, strongest relative
strength first.

Not replayable: the iNAV premium check, the 10:15-14:30 limit-order window
and the earnings blackout (historical results dates are not available).
"""

from __future__ import annotations

import math
from typing import Any, Callable

import numpy as np
import pandas as pd

from src import charges as charge_model
from src import indicators as ind
from src.strategy.simulator import Bars, Order, Trade


# --- Features and signals ---------------------------------------------------------------


def features(frame: pd.DataFrame, nifty_close: pd.Series) -> pd.DataFrame:
    """Every input the rules read, on every bar, backward-looking only."""
    f = pd.DataFrame(index=frame.index)
    for column in ("open", "high", "low", "close"):
        f[column] = pd.to_numeric(frame[column], errors="coerce")
    f["volume"] = pd.to_numeric(frame["volume"], errors="coerce") if "volume" in frame.columns else np.nan
    f["sma50"] = f["close"].rolling(50, min_periods=50).mean()
    f["ema20"] = f["close"].ewm(span=20, adjust=False, min_periods=20).mean()
    f["atr"] = ind.atr(f["high"], f["low"], f["close"], 14)
    f["vol20"] = f["volume"].rolling(20, min_periods=20).mean()
    nifty = nifty_close.reindex(f.index).ffill()
    f["rs"] = f["close"] / nifty
    f["rs_sma20"] = f["rs"].rolling(20, min_periods=20).mean()
    return f


def signals(f: pd.DataFrame, *, breadth_ok: pd.Series | None = None,
            regime_ok: pd.Series | None = None) -> tuple[np.ndarray, np.ndarray]:
    """(signal, rank) per bar. Lower rank is taken first: strongest RS."""
    n = len(f)
    close, high, low = f["close"].to_numpy(), f["high"].to_numpy(), f["low"].to_numpy()
    sma50, ema20, atr = f["sma50"].to_numpy(), f["ema20"].to_numpy(), f["atr"].to_numpy()
    volume, vol20 = f["volume"].to_numpy(), f["vol20"].to_numpy()
    rs, rs_sma = f["rs"].to_numpy(), f["rs_sma20"].to_numpy()
    has_volume = bool(np.nansum(volume) > 0)
    breadth = None if breadth_ok is None else breadth_ok.reindex(f.index).fillna(False).to_numpy(dtype=bool)
    regime = None if regime_ok is None else regime_ok.reindex(f.index).fillna(False).to_numpy(dtype=bool)

    signal = np.zeros(n, dtype=bool)
    rank = np.full(n, np.nan)
    for i in range(60, n):
        if any(math.isnan(x) for x in (close[i], sma50[i], sma50[i - 5], ema20[i], ema20[i - 5], atr[i],
                                       rs[i], rs_sma[i])):
            continue
        if not (close[i] > sma50[i] > sma50[i - 5] and ema20[i] > sma50[i] and ema20[i] > ema20[i - 5]):
            continue
        if not rs[i] > rs_sma[i]:
            continue
        if close[i] > ema20[i] + 2.0 * atr[i]:
            continue
        if not (close[i] > ema20[i] and close[i] > high[i - 1]):
            continue
        window = slice(i - 8, i)                       # the 8 sessions before today; 7 is yesterday
        if int(np.argmax(high[window])) > 5:           # the swing high is under 3 sessions old
            continue
        touch = slice(i - 7, i)
        if not np.any(low[touch] <= ema20[touch] * 1.01):
            continue
        if has_volume:
            recent = volume[i - 3:i]
            if math.isnan(vol20[i - 1]) or not np.nanmean(recent) < vol20[i - 1]:
                continue
        if breadth is not None and not breadth[i]:
            continue
        if regime is not None and not regime[i]:
            continue
        signal[i] = True
        rank[i] = -(rs[i] / rs_sma[i])
    return signal, rank


def breadth(constituents: dict[str, pd.DataFrame], index: pd.DatetimeIndex, need: int = 3) -> pd.Series:
    """True where at least `need` of the constituents closed above their 50-day SMA."""
    above = []
    for frame in constituents.values():
        close = pd.to_numeric(frame["close"], errors="coerce")
        above.append((close > close.rolling(50, min_periods=50).mean()).reindex(index).fillna(False))
    if len(above) < need:
        return pd.Series(False, index=index)
    return pd.concat(above, axis=1).sum(axis=1) >= need


def above_sma50(close: pd.Series) -> pd.Series:
    return close > close.rolling(50, min_periods=50).mean()


# --- Exits ------------------------------------------------------------------------------


class SwingRSPolicy:
    """1.5 ATR stop; at +2R trail below the 20-day EMA (2B: sell 40% there first)."""

    def __init__(self, *, partial_fraction: float = 0.0,
                 regime_ok: Callable[[Any], bool] | None = None):
        self.partial_fraction = partial_fraction
        self.regime_ok = regime_ok

    def on_open(self, trade: Trade, bars: Bars, i: int) -> None:
        entry = trade.first_price
        risk = 1.5 * trade.atr
        trade.state.update(r=risk, trailing=False, original_qty=trade.quantity)
        trade.stop = round(entry - risk, 2) if risk > 0 else None
        trade.target = round(entry + 2.0 * risk, 2) if risk > 0 else None

    def intraday(self, trade: Trade, bars: Bars, i: int) -> list[Order]:
        if not trade.state.get("trailing") and trade.stop is not None and bars.low[i] <= trade.stop:
            return [Order("SELL", "stop", price=trade.stop, kind="stop")]
        if not trade.state.get("trailing") and trade.target is not None and bars.high[i] >= trade.target:
            trade.state["trailing"] = True
            trade.stop = None                          # the trail replaces the hard stop
            qty = int(trade.state["original_qty"] * self.partial_fraction)
            if 1 <= qty < trade.quantity:
                return [Order("SELL", "partial_2R", quantity=qty, price=trade.target, kind="target")]
        return []

    def at_close(self, trade: Trade, bars: Bars, i: int) -> list[Order]:
        if self.regime_ok is not None and not self.regime_ok(bars.dates[i]):
            return [Order("SELL", "regime_switch")]
        if trade.state.get("trailing"):
            ema20 = bars.extra["ema20"][i]
            if not math.isnan(ema20) and bars.close[i] < ema20 * (1 - 0.005):
                return [Order("SELL", "trail_ema20")]
        return []


# --- Sizing and costs -------------------------------------------------------------------


def risk_sizer(risk_rs: float, max_position_rs: float, stop_atr: float = 1.5):
    """Rupees to put in: risk / stop distance, capped. Zero without an ATR."""
    def size(symbol: str, bars: Bars, signal_i: int, price: float) -> float:
        atr = float(bars.extra["atr"][signal_i]) if "atr" in bars.extra else float("nan")
        if math.isnan(atr) or atr <= 0 or price <= 0:
            return 0.0
        units = risk_rs / (stop_atr * atr)
        return min(units * price, max_position_rs)
    return size


def etf_costs(cfg: Any):
    """Groww delivery charges for ETFs: no STT on buys, 0.001% on equity-ETF
    sells and none on gold/silver ETFs (modelled at 0.001% for both, which
    differs by Rs 1 on Rs 1 lakh). Brokerage, stamp duty, exchange, SEBI, GST
    and DP are as for shares."""
    def cost(side: str, qty: int, price: float) -> float:
        breakdown = charge_model.estimate(side, qty, price, cfg=cfg)
        stt = qty * price * 0.00001 if side == "SELL" else 0.0
        return breakdown.total - breakdown.stt + stt
    return cost
