"""Does the Indian dip strategy work on US stocks and ETFs, after Groww's costs?

Procedure, fixed before any result was seen:

1. The live dip rules (config `dip:`) are run unchanged on the S&P 500 and
   Nasdaq-100, checked every 5 sessions, with the live exit plan (L2: -25%
   stop, a quarter sold at +25% and +50%, a 20% trail after +30%). Portfolio:
   $100,000, 8% positions, at most 15, filled at the next session's open,
   with Groww's US charges and a 0.5% currency conversion on every order.
2. Two versions: the dip rules alone, and the dip rules plus a stand-in for
   India's delivery confirmation (volume on down days running above its
   20-day average, with the same thresholds). The US publishes no delivery
   data, so this is the closest free equivalent; heavy volume on down days
   can mean buyers absorbing the selling or holders leaving, and only the
   test can say which dominates.
3. Each is judged on 2014-2021 and on 2022 onward against holding SPY
   (dividends reinvested, no costs). The 30 ETFs in config are judged the
   same way with 5 positions of 20%.
4. A version passes only if every criterion in `PASS` holds in both periods.
   Nothing is loosened after the fact.

Survivorship bias is worse here than in India: the S&P 500 adds companies
after they have grown, so today's list is a list of past winners. A marginal
pass should be read as a fail.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from src.strategy import research, swing
from src.strategy.simulator import Bars, curve_stats, trade_stats

log = logging.getLogger(__name__)

PERIODS = (("2014-21", research.TRAIN[0], research.TRAIN[1]),
           ("2022+", research.TEST_START, None))

#: Pre-registered. Both periods, after costs, at next-open entry.
PASS = {
    "beat_benchmark_cagr": True,          # growth above holding SPY
    "drawdown_no_worse": True,            # worst fall no deeper than SPY's
    "min_trades_stocks": 50,              # closed trades per period
    "min_trades_etfs": 15,
}


# --- The delivery stand-in --------------------------------------------------------------


def down_volume_confirmed(frame: pd.DataFrame, *, window: int = 20, lookback: int = 10,
                          ratio_min: float = 1.05, persistence: int = 2) -> pd.Series:
    """Point-in-time: on each bar, would the volume version of Stage 3 pass?

    Mirrors screener's delivery test with volume in place of delivery %:
    the mean of the last ten down-day ratios must reach `ratio_min`, and at
    least `persistence` of the last `lookback` sessions must be down days at
    or above it. Every input is backward-looking.
    """
    close = pd.to_numeric(frame["close"], errors="coerce")
    volume = pd.to_numeric(frame["volume"], errors="coerce")
    average = volume.rolling(window=window, min_periods=max(2, window // 2)).mean()
    ratio = (volume / average.replace(0.0, np.nan)).where(close.diff() < 0)

    down = ratio.dropna()
    mean_recent = down.rolling(10, min_periods=1).mean().reindex(ratio.index).ffill()
    count = (ratio >= ratio_min).astype(int).rolling(lookback, min_periods=1).sum()
    return ((mean_recent >= ratio_min) & (count >= persistence)).fillna(False)


def confirm_signals(signals: dict[str, np.ndarray], bars: dict[str, Bars],
                    prices: dict[str, pd.DataFrame], cfg: Any) -> dict[str, np.ndarray]:
    """The dip signals that the volume stand-in also confirms."""
    out = {}
    for symbol, signal in signals.items():
        raw = prices.get(symbol)
        if raw is None or "volume" not in raw.columns:
            out[symbol] = np.zeros_like(signal)
            continue
        mask = down_volume_confirmed(
            raw,
            window=int(cfg.get("delivery.avg_window_days", 20)),
            lookback=int(cfg.get("us.research.down_volume_lookback", 10)),
            ratio_min=float(cfg.get("us.research.down_volume_ratio_min", 1.05)),
            persistence=int(cfg.get("us.research.down_volume_persistence", 2)),
        )
        aligned = mask.reindex(pd.DatetimeIndex(bars[symbol].dates), fill_value=False).to_numpy(dtype=bool)
        out[symbol] = signal & aligned
    return out


# --- Benchmarks -------------------------------------------------------------------------


def total_return(frame: pd.DataFrame | None) -> pd.DataFrame | None:
    """Dividends reinvested, so the benchmark is not handicapped by ~1.3% a year."""
    return None if frame is None or frame.empty else swing.adjust_for_dividends(frame)


def in_rupees(curve: pd.Series, usdinr: pd.DataFrame | None) -> pd.Series:
    """A dollar equity curve valued in rupees on each day."""
    if usdinr is None or usdinr.empty or curve.empty:
        return pd.Series(dtype=float)
    rate = usdinr["close"].copy()
    rate.index = [d.date() for d in rate.index]
    rate = rate.reindex(sorted(set(rate.index) | set(curve.index))).ffill()
    return (curve * rate.reindex(curve.index)).dropna()


# --- One version, both periods ----------------------------------------------------------


@dataclass
class Outcome:
    key: str
    label: str
    periods: dict[str, dict[str, Any]] = field(default_factory=dict)
    passed: bool = False
    reasons: list[str] = field(default_factory=list)
    trades: list[dict[str, Any]] = field(default_factory=list)


def run_version(key: str, label: str, plan, bars, signals, ranks, *, today: date, cfg, regime_at,
                benchmark: pd.DataFrame | None, cost_fn, capital: float, position_pct: float,
                max_positions: int, min_trades: int) -> Outcome:
    outcome = Outcome(key, label)
    for name, start, end in PERIODS:
        end = end or today
        result = research.run_long_term(plan, bars, signals, ranks, start=start, end=end, cfg=cfg,
                                        regime_at=regime_at, capital=capital,
                                        position_pct=position_pct, max_positions=max_positions,
                                        cost_fn=cost_fn)
        curve = curve_stats(result.equity, result.initial_capital)
        curve["exposure"] = result.exposure
        stats = trade_stats(result.trades)
        bench = curve_stats(research.benchmark_curve(benchmark, start, end, capital), capital)
        outcome.periods[name] = {"curve": curve, "trades": research._brief(stats),
                                 "by_regime": stats.get("by_regime", {}), "by_exit": stats.get("by_exit", {}),
                                 "benchmark": bench, "equity": result.equity}
        outcome.trades += [dict(t.to_row(), period=name) for t in result.trades]
    return judge(outcome, min_trades)


def judge(outcome: Outcome, min_trades: int) -> Outcome:
    reasons = []
    for name, p in outcome.periods.items():
        cagr, bench_cagr = p["curve"]["cagr_pct"], p["benchmark"]["cagr_pct"]
        dd, bench_dd = p["curve"]["max_drawdown_pct"], p["benchmark"]["max_drawdown_pct"]
        n = p["trades"].get("trades") or 0
        if n < min_trades:
            reasons.append(f"{name}: only {n} closed trades (need {min_trades})")
        if cagr <= bench_cagr:
            reasons.append(f"{name}: grew {cagr:.1f}% a year, not above SPY's {bench_cagr:.1f}%")
        if dd < bench_dd:
            reasons.append(f"{name}: worst fall {dd:.1f}%, deeper than SPY's {bench_dd:.1f}%")
    outcome.reasons = reasons
    outcome.passed = not reasons
    return outcome


def recommend(outcomes: list[Outcome]) -> tuple[str | None, str]:
    """The passing version with the higher growth since 2022, or none."""
    passing = [o for o in outcomes if o.passed]
    if not passing:
        return None, "No version beat holding SPY on both growth and worst fall in both periods."
    best = max(passing, key=lambda o: o.periods["2022+"]["curve"]["cagr_pct"])
    return best.key, f"{best.label} passed in both periods and grew fastest since 2022."
