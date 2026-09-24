"""Swing trading rules: published short-term mean reversion, as tested.

Every rule here comes from published work and is used with its published
parameters. None were tuned to this market. They share one idea: inside a
long-term uptrend (close above the 200-day average), a sharp two-to-seven
day pullback tends to snap back within days.

This module is the single source of truth for the rules. The research
backtest and the live morning scan both call it, so the rule that sends an
alert is the rule that was tested - the same discipline as
`screener.evaluate_dip` and `backtest.dip_signal_at`.

Everything is computed backwards-only (rolling windows and shifts), so the
value on any bar uses nothing after it. `tests/test_swing.py` checks that by
appending future bars and confirming past signals do not change.

Sources, cited in reports/strategy_research.md:
  A  RSI(2)            Connors & Alvarez, "Short Term Trading Strategies That Work" (2008)
  B  Cumulative RSI(2) same book
  C  Double 7s         Connors & Alvarez, "Short Term Trading Strategies That Work"
  D  IBS               Internal Bar Strength; Pagonidis, and QuantifiedStrategies tests
  E  TPS               Connors, "High Probability ETF Trading" (2009)
  F  Nifty losers      short-lookback cross-sectional reversal on the Nifty 50
                       (delphicalpha, 2026), the one India-specific study found
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

from src import indicators as ind

#: Bars needed before any rule can fire: the 200-day average plus slack.
MIN_HISTORY = 205


def adjust_for_dividends(ohlc: pd.DataFrame) -> pd.DataFrame:
    """Scale open/high/low/close by adj_close / close.

    Yahoo's prices are split-adjusted but not dividend-adjusted. Without this
    the drop on an ex-dividend date reads as a sharp one-day selloff and
    fires every oversold rule on stocks like Coal India or ITC. The factor is
    1.0 on the latest bar, so today's prices - the ones an alert quotes - are
    unchanged.
    """
    out = ohlc.copy()
    if "adj_close" not in out.columns:
        return out
    factor = (out["adj_close"] / out["close"]).replace([np.inf, -np.inf], np.nan).fillna(1.0)
    for column in ("open", "high", "low", "close"):
        if column in out.columns:
            out[column] = out[column] * factor
    return out


def features(ohlc: pd.DataFrame) -> pd.DataFrame:
    """Every input the swing rules read, on every bar."""
    frame = adjust_for_dividends(ohlc)
    f = pd.DataFrame(index=frame.index)
    for column in ("open", "high", "low", "close"):
        f[column] = pd.to_numeric(frame[column], errors="coerce")

    close = f["close"]
    f["sma200"] = ind.sma(close, 200)
    f["sma5"] = ind.sma(close, 5)
    f["rsi2"] = ind.rsi(close, 2)
    f["cum_rsi2"] = f["rsi2"] + f["rsi2"].shift(1)
    day_range = f["high"] - f["low"]
    f["ibs"] = ((close - f["low"]) / day_range).where(day_range > 0)
    f["low7"] = close.rolling(7, min_periods=7).min()
    f["high7"] = close.rolling(7, min_periods=7).max()
    f["prev_low"] = f["low"].shift(1)
    f["prev_high"] = f["high"].shift(1)
    f["atr14"] = ind.atr(f["high"], f["low"], close, 14)
    f["atr22"] = ind.atr(f["high"], f["low"], close, 22)
    f["ret5"] = (close / close.shift(5) - 1.0) * 100.0
    f["uptrend"] = (close > f["sma200"]).fillna(False)
    return f


@dataclass(frozen=True)
class SwingRule:
    key: str
    name: str
    entry_text: str
    exit_text: str
    source: str
    max_hold: int
    entry: Callable[[pd.DataFrame], pd.Series]
    exit: Callable[[pd.DataFrame], pd.Series]
    rank: Callable[[pd.DataFrame], pd.Series]   # lower = more oversold = taken first
    scale_in: tuple[float, ...] = (1.0,)
    cross_sectional: bool = False

    def entries(self, f: pd.DataFrame) -> pd.Series:
        return self.entry(f).fillna(False).astype(bool)

    def exits(self, f: pd.DataFrame) -> pd.Series:
        return self.exit(f).fillna(False).astype(bool)


def _never(f: pd.DataFrame) -> pd.Series:
    return pd.Series(False, index=f.index)


RULES: dict[str, SwingRule] = {
    "rsi2": SwingRule(
        key="rsi2", name="RSI(2) pullback",
        entry_text="close above the 200-day average and RSI(2) below 10",
        exit_text="close above the 5-day average",
        source="Connors & Alvarez, Short Term Trading Strategies That Work (2008)",
        max_hold=10,
        entry=lambda f: f["uptrend"] & (f["rsi2"] < 10),
        exit=lambda f: f["close"] > f["sma5"],
        rank=lambda f: f["rsi2"],
    ),
    "cum_rsi2": SwingRule(
        key="cum_rsi2", name="Cumulative RSI(2)",
        entry_text="close above the 200-day average and the last two RSI(2) readings summing below 35",
        exit_text="RSI(2) above 65",
        source="Connors & Alvarez, Short Term Trading Strategies That Work (2008)",
        max_hold=10,
        entry=lambda f: f["uptrend"] & (f["cum_rsi2"] < 35),
        exit=lambda f: f["rsi2"] > 65,
        rank=lambda f: f["cum_rsi2"],
    ),
    "double7": SwingRule(
        key="double7", name="Double 7s",
        entry_text="close above the 200-day average and the lowest close of the last 7 days",
        exit_text="the highest close of the last 7 days",
        source="Connors & Alvarez, Short Term Trading Strategies That Work (2008)",
        max_hold=10,
        entry=lambda f: f["uptrend"] & (f["close"] <= f["low7"]),
        exit=lambda f: f["close"] >= f["high7"],
        rank=lambda f: f["rsi2"],
    ),
    "ibs": SwingRule(
        key="ibs", name="IBS reversal",
        entry_text="close above the 200-day average, in the bottom 20% of the day's range, below the previous low",
        exit_text="close in the top 20% of the day's range, or above the previous high",
        source="Internal Bar Strength (Pagonidis); QuantifiedStrategies backtests",
        max_hold=10,
        entry=lambda f: f["uptrend"] & (f["ibs"] < 0.2) & (f["close"] < f["prev_low"]),
        exit=lambda f: (f["ibs"] > 0.8) | (f["close"] > f["prev_high"]),
        rank=lambda f: f["ibs"],
    ),
    "tps": SwingRule(
        key="tps", name="TPS scale-in",
        entry_text="close above the 200-day average and RSI(2) below 25 two days running; "
                   "adds 20/30/40% more on each lower close",
        exit_text="RSI(2) above 70",
        source="Connors, High Probability ETF Trading (2009)",
        max_hold=20,
        entry=lambda f: f["uptrend"] & (f["rsi2"] < 25) & (f["rsi2"].shift(1) < 25),
        exit=lambda f: f["rsi2"] > 70,
        rank=lambda f: f["rsi2"],
        scale_in=(0.1, 0.2, 0.3, 0.4),
    ),
    "losers": SwingRule(
        key="losers", name="Nifty 5-day losers",
        entry_text="weekly: the 5 Nifty 50 stocks above their 200-day average that fell most over 5 days",
        exit_text="held 20 sessions",
        source="Cross-sectional reversal on the Nifty 50 (delphicalpha, 2026)",
        max_hold=20,
        entry=_never,      # cross-sectional: built by losers_entries()
        exit=_never,
        rank=lambda f: f["ret5"],
        cross_sectional=True,
    ),
}


def losers_entries(
    frames: dict[str, pd.DataFrame], *, every: int = 5, count: int = 5
) -> dict[str, pd.Series]:
    """Weekly, the `count` weakest 5-day performers among uptrending stocks.

    Built across all stocks at once because the rule is a ranking. Each date
    ranks only the stocks trading that day, using only that day's values.
    """
    calendar = sorted({d for f in frames.values() for d in f.index})
    rebalance = set(calendar[::every])
    out = {symbol: pd.Series(False, index=f.index) for symbol, f in frames.items()}

    returns = pd.DataFrame({s: f["ret5"] for s, f in frames.items()})
    eligible = pd.DataFrame({s: f["uptrend"] for s, f in frames.items()}).fillna(False)

    for day in calendar:
        if day not in rebalance or day not in returns.index:
            continue
        row = returns.loc[day].where(eligible.loc[day].astype(bool)).dropna()
        for symbol in row.nsmallest(count).index:
            out[symbol].loc[day] = True
    return out


# --- Exit plans -------------------------------------------------------------------


@dataclass(frozen=True)
class ExitPlan:
    """How a swing trade is closed: stop, target, and a time limit.

    `stop_atr=None` means no price stop - how Connors published these rules,
    with only the time stop protecting the trade. `target_atr=None` means the
    rule's own exit (for example "close above the 5-day average") decides.
    """

    stop_atr: float | None
    target_atr: float | None
    max_hold: int

    @property
    def label(self) -> str:
        stop = f"stop {self.stop_atr:g}xATR" if self.stop_atr else "no stop"
        target = f"target {self.target_atr:g}xATR" if self.target_atr else "rule exit"
        return f"{stop}, {target}, {self.max_hold}d"

    def levels(self, entry_price: float, atr: float) -> tuple[float | None, float | None]:
        """Stop and target prices for a fill at `entry_price`."""
        stop = round(entry_price - self.stop_atr * atr, 2) if self.stop_atr and atr > 0 else None
        target = round(entry_price + self.target_atr * atr, 2) if self.target_atr and atr > 0 else None
        return stop, target


STOP_CHOICES: tuple[float | None, ...] = (None, 2.0, 3.0)
TARGET_CHOICES: tuple[float | None, ...] = (None, 1.0, 2.0)


def plan_grid(rule: SwingRule) -> list[ExitPlan]:
    """The nine stop/target combinations each rule is tested with."""
    return [ExitPlan(s, t, rule.max_hold) for s in STOP_CHOICES for t in TARGET_CHOICES]
