"""Technical indicators.

Pure functions over pandas Series/DataFrames - no I/O, no config, no network.
That makes them trivially testable, which matters because every downstream
decision in this system rests on these numbers being right.

A note on smoothing: RSI and ATR use Wilder's method, seeded with a simple
mean of the first `period` observations and then smoothed recursively. The
shortcut of running an EWM from the very first bar converges to the same
value but differs for the first hundred-odd bars, which is exactly the range
a 6-year history spends warming up. We want figures that match NSE and
screener.in exactly, not approximately.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# Sessions per year on NSE, after weekends and ~15 trading holidays.
TRADING_DAYS_YEAR = 252


# --- Smoothing primitives ---------------------------------------------------


def wilder_smooth(series: pd.Series, period: int) -> pd.Series:
    """Wilder's recursive smoothing, seeded with the first simple average.

    avg[t] = (avg[t-1] * (period - 1) + value[t]) / period
    """
    values = pd.to_numeric(series, errors="coerce").to_numpy(dtype=float)
    out = np.full(len(values), np.nan, dtype=float)

    if len(values) < period or period < 1:
        return pd.Series(out, index=series.index, name=series.name)

    seed = np.nanmean(values[:period])
    if np.isnan(seed):
        return pd.Series(out, index=series.index, name=series.name)

    out[period - 1] = seed
    for i in range(period, len(values)):
        current = values[i]
        if np.isnan(current):
            out[i] = out[i - 1]
            continue
        out[i] = (out[i - 1] * (period - 1) + current) / period

    return pd.Series(out, index=series.index, name=series.name)


def sma(series: pd.Series, window: int) -> pd.Series:
    """Simple moving average. Requires a full window, so no partial averages."""
    return pd.to_numeric(series, errors="coerce").rolling(window=window, min_periods=window).mean()


# --- Momentum ---------------------------------------------------------------


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's Relative Strength Index.

    Returns 100 where there have been no losses in the window (RS is infinite)
    rather than NaN, which is the conventional reading of a pure uptrend.
    """
    close = pd.to_numeric(close, errors="coerce")
    delta = close.diff()

    gains = delta.clip(lower=0.0)
    losses = (-delta).clip(lower=0.0)

    # Drop the leading NaN from .diff() so the seed average covers `period`
    # real observations rather than period-1 plus a NaN.
    avg_gain = wilder_smooth(gains.iloc[1:], period).reindex(close.index)
    avg_loss = wilder_smooth(losses.iloc[1:], period).reindex(close.index)

    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    result = 100.0 - (100.0 / (1.0 + rs))

    # avg_loss == 0 with a valid avg_gain means an unbroken run of gains.
    no_loss = (avg_loss == 0.0) & avg_gain.notna()
    result = result.mask(no_loss, 100.0)
    # Both zero means a completely flat stretch; RSI is conventionally 50.
    flat = (avg_loss == 0.0) & (avg_gain == 0.0)
    result = result.mask(flat, 50.0)

    return result.rename("rsi")


# --- Volatility -------------------------------------------------------------


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """max(H-L, |H-prevC|, |L-prevC|) - the first bar falls back to H-L."""
    high = pd.to_numeric(high, errors="coerce")
    low = pd.to_numeric(low, errors="coerce")
    prev_close = pd.to_numeric(close, errors="coerce").shift(1)

    ranges = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1, skipna=True).rename("true_range")


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Average True Range, Wilder-smoothed. Absolute rupees, not a percentage."""
    return wilder_smooth(true_range(high, low, close), period).rename("atr")


def atr_pct(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """ATR as a percentage of price - comparable across stocks of any price."""
    close = pd.to_numeric(close, errors="coerce")
    return (atr(high, low, close, period) / close * 100.0).rename("atr_pct")


# --- Trend ------------------------------------------------------------------


def dma_slope_pct(dma: pd.Series, lookback: int) -> pd.Series:
    """Percentage change in a moving average over `lookback` sessions.

    This is the guard that separates a dip inside an uptrend from the early
    part of a collapse. Price can sit above a 200 DMA that is itself rolling
    over; requiring a non-negative slope rejects exactly that case.
    """
    dma = pd.to_numeric(dma, errors="coerce")
    past = dma.shift(lookback)
    return ((dma - past) / past.replace(0.0, np.nan) * 100.0).rename("dma_slope_pct")


def rolling_high(series: pd.Series, window: int = TRADING_DAYS_YEAR) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").rolling(window=window, min_periods=1).max()


def rolling_low(series: pd.Series, window: int = TRADING_DAYS_YEAR) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").rolling(window=window, min_periods=1).min()


def drawdown_from_high_pct(close: pd.Series, window: int = TRADING_DAYS_YEAR) -> pd.Series:
    """Percentage below the rolling high. Positive means below the high.

    A value of 18.0 reads as "18% off its 52-week high".
    """
    close = pd.to_numeric(close, errors="coerce")
    high = rolling_high(close, window)
    return ((high - close) / high.replace(0.0, np.nan) * 100.0).rename("drawdown_pct")


def swing_lows(low: pd.Series, order: int = 5) -> pd.Series:
    """Boolean mask of local minima: lows below the `order` bars either side.

    Used to find prior support levels. `order=5` keeps only pivots that held
    for a week on both sides, which filters out single-session noise.
    """
    low = pd.to_numeric(low, errors="coerce")
    values = low.to_numpy(dtype=float)
    mask = np.zeros(len(values), dtype=bool)

    for i in range(order, len(values) - order):
        window = values[i - order : i + order + 1]
        if np.isnan(window).any():
            continue
        if values[i] == window.min() and (window[:order] > values[i]).all() and (window[order + 1 :] > values[i]).all():
            mask[i] = True

    return pd.Series(mask, index=low.index, name="swing_low")


def nearest_support(
    close_price: float,
    low: pd.Series,
    lookback: int = TRADING_DAYS_YEAR,
    order: int = 5,
) -> float | None:
    """The highest swing low sitting at or below the current price.

    That is the first shelf the price would have to break through, which is
    the level worth measuring proximity against.
    """
    recent = low.iloc[-lookback:] if len(low) > lookback else low
    pivots = recent[swing_lows(recent, order)]
    below = pivots[pivots <= close_price]
    if below.empty:
        return None
    return float(below.max())


# --- Delivery (NSE-specific) ------------------------------------------------


def delivery_pct(deliverable_qty: pd.Series, traded_qty: pd.Series) -> pd.Series:
    """Deliverable quantity as a percentage of total traded quantity.

    NSE publishes both in the daily bhavcopy. High delivery means shares
    actually settled into demat accounts instead of being squared off
    intraday - the difference between ownership changing hands and churn.
    """
    deliverable = pd.to_numeric(deliverable_qty, errors="coerce")
    traded = pd.to_numeric(traded_qty, errors="coerce")
    return (deliverable / traded.replace(0.0, np.nan) * 100.0).rename("delivery_pct")


def down_day_delivery_ratio(
    close: pd.Series,
    delivery_percentage: pd.Series,
    window: int = 20,
) -> pd.Series:
    """Delivery % on down days relative to its own rolling average.

    The core of Stage 3. A reading above 1.0 means that when the price fell,
    a larger-than-usual share of the volume was settled rather than churned -
    long-term holders absorbing the selling. On up days this is NaN, because
    the signal only carries meaning on a decline.
    """
    close = pd.to_numeric(close, errors="coerce")
    delivery = pd.to_numeric(delivery_percentage, errors="coerce")

    avg = delivery.rolling(window=window, min_periods=max(2, window // 2)).mean()
    ratio = delivery / avg.replace(0.0, np.nan)

    is_down_day = close.diff() < 0
    return ratio.where(is_down_day).rename("down_day_delivery_ratio")


def delivery_persistence(
    close: pd.Series,
    delivery_percentage: pd.Series,
    window: int = 20,
    lookback: int = 10,
    ratio_threshold: float = 1.10,
) -> int:
    """Count of recent down days with elevated delivery.

    A single block deal can spike one session's delivery percentage and look
    exactly like accumulation. Requiring the elevation to repeat across
    several sessions is what separates the two.
    """
    ratio = down_day_delivery_ratio(close, delivery_percentage, window)
    recent = ratio.iloc[-lookback:] if len(ratio) > lookback else ratio
    return int((recent >= ratio_threshold).sum())


# --- Convenience ------------------------------------------------------------


def compute_indicator_frame(
    df: pd.DataFrame,
    *,
    rsi_period: int = 14,
    atr_period: int = 14,
    dma_long: int = 200,
    dma_short: int = 50,
    slope_lookback: int = 126,
    delivery_window: int = 20,
) -> pd.DataFrame:
    """Attach every indicator the screener needs to an OHLCV frame.

    Expects lowercase columns: open, high, low, close, volume, and optionally
    deliverable_qty. Returns a copy; the input is never mutated.
    """
    required = {"high", "low", "close"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"OHLCV frame missing required columns: {sorted(missing)}")

    out = df.copy()

    out["rsi"] = rsi(out["close"], rsi_period)
    out["atr"] = atr(out["high"], out["low"], out["close"], atr_period)
    out["atr_pct"] = out["atr"] / out["close"] * 100.0

    out[f"dma_{dma_long}"] = sma(out["close"], dma_long)
    out[f"dma_{dma_short}"] = sma(out["close"], dma_short)
    out["dma_long_slope_pct"] = dma_slope_pct(out[f"dma_{dma_long}"], slope_lookback)

    out["high_52w"] = rolling_high(out["close"], TRADING_DAYS_YEAR)
    out["low_52w"] = rolling_low(out["close"], TRADING_DAYS_YEAR)
    out["drawdown_pct"] = drawdown_from_high_pct(out["close"], TRADING_DAYS_YEAR)

    if "deliverable_qty" in out.columns and "volume" in out.columns:
        out["delivery_pct"] = delivery_pct(out["deliverable_qty"], out["volume"])
        out["delivery_avg"] = out["delivery_pct"].rolling(
            window=delivery_window, min_periods=max(2, delivery_window // 2)
        ).mean()
        out["down_day_delivery_ratio"] = down_day_delivery_ratio(
            out["close"], out["delivery_pct"], delivery_window
        )

    return out
