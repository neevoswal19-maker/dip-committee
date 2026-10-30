"""Correction buying: buy what fell in a market correction, sell at its old high.

Rules fixed on 2026-10-30, before any test (see reports/correction_research.md):

- **Episode:** the index closes X% below its running high (X = 10, 15 or 20).
  No new trigger until the index makes a new high.
- **Pre-correction high** of a stock: its highest close from 63 sessions
  before the index's peak up to the trigger day.
- **Selection:** stocks further below their pre-correction high than the
  index is below its own, with at least two years of history.
- **Trade:** buy at the next session's open; sell at the pre-correction high
  (a limit order), or after 504 sessions (two years); variant E2 also stops
  out at -25% from entry.
- **Benchmark:** buy the index at the same open and sell when it regains its
  peak, or after two years.

Every function reads only data up to the day it decides on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

LEAD_SESSIONS = 63
MIN_HISTORY = 504
MAX_HOLD = 504

#: Fixed before running. Judged only on episodes whose two-year window has
#: closed; the rest are reported as in progress.
PASS = {"min_episodes": 4, "beat_share": 2 / 3, "min_target_rate": 0.60}


@dataclass
class Episode:
    depth: float
    peak_date: pd.Timestamp
    peak_level: float
    trigger_date: pd.Timestamp
    trigger_level: float
    recovered_date: pd.Timestamp | None = None

    @property
    def index_drawdown(self) -> float:
        return self.trigger_level / self.peak_level - 1.0


def episodes(close: pd.Series, depth_pct: float) -> list[Episode]:
    """Every correction of at least depth_pct, one per fall from a high."""
    close = close.dropna()
    out: list[Episode] = []
    if close.empty:
        return out
    peak_level, peak_date, armed = float(close.iloc[0]), close.index[0], True
    current: Episode | None = None
    for ts, value in close.items():
        value = float(value)
        if value >= peak_level:
            if current is not None and current.recovered_date is None:
                current.recovered_date = ts
            peak_level, peak_date, armed = value, ts, True
            continue
        if armed and value / peak_level - 1.0 <= -depth_pct / 100.0:
            current = Episode(depth_pct, peak_date, peak_level, ts, value)
            out.append(current)
            armed = False
    return out


def pre_correction_high(close: pd.Series, peak_date: pd.Timestamp, trigger_date: pd.Timestamp,
                        lead: int = LEAD_SESSIONS) -> float | None:
    """The stock's highest close from `lead` sessions before the index peak to the trigger."""
    close = close.dropna()
    start = max(0, int(close.index.searchsorted(peak_date)) - lead)
    end = int(close.index.searchsorted(trigger_date, side="right"))
    window = close.iloc[start:end]
    return float(window.max()) if not window.empty else None


def selected(frame: pd.DataFrame, episode: Episode) -> dict[str, float] | None:
    """The stock's target and its fall, if it fell more than the index; else None."""
    close = frame["close"].dropna()
    history = close[close.index <= episode.trigger_date]
    if len(history) < MIN_HISTORY:
        return None
    high = pre_correction_high(close, episode.peak_date, episode.trigger_date)
    if not high:
        return None
    fall = float(history.iloc[-1]) / high - 1.0
    if fall >= episode.index_drawdown:          # fell less than the index
        return None
    return {"target": high, "fall": fall}


@dataclass
class TradeResult:
    symbol: str
    entry_date: pd.Timestamp
    entry: float
    target: float
    exit_date: pd.Timestamp
    exit: float
    reason: str                                  # target | stop | time | open
    sessions: int
    gross_pct: float
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def reached(self) -> bool:
        return self.reason == "target"


def trade(frame: pd.DataFrame, symbol: str, trigger_date: pd.Timestamp, target: float, *,
          stop_pct: float | None, max_hold: int = MAX_HOLD, slippage: float = 0.0005) -> TradeResult | None:
    """Buy at the next open after the trigger; walk forward to an exit."""
    frame = frame.dropna(subset=["open", "high", "low", "close"])
    start = int(frame.index.searchsorted(trigger_date, side="right"))
    if start >= len(frame):
        return None
    opens, highs = frame["open"].to_numpy(float), frame["high"].to_numpy(float)
    lows, closes = frame["low"].to_numpy(float), frame["close"].to_numpy(float)
    entry_date = frame.index[start]
    entry = opens[start] * (1 + slippage)
    stop = entry * (1 - stop_pct / 100.0) if stop_pct else None
    last = min(len(frame) - 1, start + max_hold - 1)
    for i in range(start, last + 1):
        if stop is not None and lows[i] <= stop:     # the stop is assumed first
            price = min(opens[i], stop) * (1 - slippage)
            return _done(symbol, entry_date, entry, target, frame.index[i], price, "stop", i - start + 1)
        if highs[i] >= target:
            price = max(opens[i], target)            # a resting limit: no slippage
            return _done(symbol, entry_date, entry, target, frame.index[i], price, "target", i - start + 1)
    reason = "time" if last == start + max_hold - 1 else "open"
    price = closes[last] * (1 - slippage if reason == "time" else 1)
    return _done(symbol, entry_date, entry, target, frame.index[last], float(price), reason, last - start + 1)


def _done(symbol, entry_date, entry, target, exit_date, price, reason, sessions) -> TradeResult:
    return TradeResult(symbol, entry_date, entry, target, exit_date, price, reason, sessions,
                       (price / entry - 1.0) * 100.0)


def index_trade(close: pd.Series, open_: pd.Series, episode: Episode, max_hold: int = MAX_HOLD) -> dict[str, Any]:
    """The benchmark: buy the index at the same open, sell when it regains its peak."""
    start = int(close.index.searchsorted(episode.trigger_date, side="right"))
    if start >= len(close):
        return {"return_pct": None, "sessions": 0, "reached": False}
    entry = float(open_.iloc[start]) if not np.isnan(open_.iloc[start]) else float(close.iloc[start])
    last = min(len(close) - 1, start + max_hold - 1)
    for i in range(start, last + 1):
        if float(close.iloc[i]) >= episode.peak_level:
            return {"return_pct": (float(close.iloc[i]) / entry - 1) * 100, "sessions": i - start + 1, "reached": True}
    return {"return_pct": (float(close.iloc[last]) / entry - 1) * 100, "sessions": last - start + 1, "reached": False}


def basket(trades: list[TradeResult], costs_fn, capital: float) -> dict[str, Any]:
    """Equal money in each trade; net result after both legs' charges."""
    if not trades:
        return {"trades": 0}
    each = capital / len(trades)
    cost = proceeds = 0.0
    for t in trades:
        qty = max(1, int(each // t.entry))
        cost += qty * t.entry + costs_fn("BUY", qty, t.entry)
        proceeds += qty * t.exit - costs_fn("SELL", qty, t.exit)
    net_pct = (proceeds / cost - 1.0) * 100.0
    years = max(np.mean([t.sessions for t in trades]) / 252.0, 1 / 252)
    return {
        "trades": len(trades), "net_pct": net_pct,
        "per_year_pct": ((1 + net_pct / 100.0) ** (1 / years) - 1) * 100.0 if net_pct > -100 else -100.0,
        "target_rate": sum(t.reached for t in trades) / len(trades),
        "stopped": sum(t.reason == "stop" for t in trades), "open": sum(t.reason == "open" for t in trades),
        "avg_sessions": float(np.mean([t.sessions for t in trades])),
    }


def judge(rows: list[dict[str, Any]]) -> tuple[bool, list[str]]:
    """rows: one per judged episode with basket and index results."""
    reasons = []
    n = len(rows)
    if n < PASS["min_episodes"]:
        reasons.append(f"only {n} completed episodes (need {PASS['min_episodes']})")
    if n:
        beats = sum(1 for r in rows if r["basket_pct"] > r["index_pct"])
        if beats / n < PASS["beat_share"]:
            reasons.append(f"beat the index in {beats} of {n} episodes (need two-thirds)")
        gap = float(np.mean([r["basket_pct"] - r["index_pct"] for r in rows]))
        if gap <= 0:
            reasons.append(f"averaged {gap:+.1f} points against the index")
        trades = sum(r["trades"] for r in rows)
        hit = sum(r["target_rate"] * r["trades"] for r in rows) / trades if trades else 0.0
        if hit < PASS["min_target_rate"]:
            reasons.append(f"{hit:.0%} of trades reached their old high within two years (need 60%)")
    return not reasons, reasons
