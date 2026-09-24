"""The strategy research: which swing rules, stops and targets are worth using.

Procedure, fixed before any result was seen:

1. Each published swing rule is run with each of nine stop/target plans on
   the Nifty 50 over **2014-2021 only**, after Groww costs, entering at the
   next morning's open. The plan with the best profit factor among those
   winning at least 60% of trades is chosen; if none reaches 60%, the best
   profit factor is taken anyway, and it will fail the gate below.
2. That choice is then tested untouched on **2022 onwards**, on the **Nifty
   Next 50** (stocks the choice never saw), and as a **5-slot portfolio** of
   Rs 50,000 positions against simply holding the Nifty 50.
3. It passes only if every criterion in `PASS` holds. Nothing is loosened
   after the fact.

Survivorship bias applies: both universes are today's members, and stocks
that fell out of the index are missing. That flatters every result here, so
a marginal pass should be read as a fail.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from src.strategy import swing
from src.strategy.exit_policies import LongTermPlan, LongTermPolicy, SwingPolicy
from src.strategy.simulator import (
    NEXT_OPEN, SAME_CLOSE, Bars, SimResult, curve_stats, simulate, trade_stats,
)

log = logging.getLogger(__name__)

TRAIN = (date(2014, 1, 1), date(2021, 12, 31))
TEST_START = date(2022, 1, 1)

#: Pre-registered pass criteria, all after costs, all at next-open entry.
PASS = {
    "min_win_rate": 0.60,          # both periods
    "min_profit_factor": 1.30,     # both periods
    "next50_min_win_rate": 0.55,
    "next50_min_profit_factor": 1.10,
    "min_train_trades": 100,
    "min_test_trades": 50,
}

POSITION_VALUE = 50_000.0
MAX_SWING_POSITIONS = 5


# --- Data preparation ------------------------------------------------------------------


def swing_features(prices: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    out = {}
    for symbol, frame in prices.items():
        if frame is None or len(frame) < swing.MIN_HISTORY:
            continue
        try:
            out[symbol] = swing.features(frame)
        except Exception as exc:
            log.warning("Features failed for %s: %s", symbol, exc)
    return out


def to_bars(features: dict[str, pd.DataFrame]) -> dict[str, Bars]:
    bars = {}
    for symbol, f in features.items():
        frame = f.rename(columns={"atr14": "atr"})
        bars[symbol] = Bars.from_frame(symbol, frame, extra=("atr", "atr22"))
    return bars


def regime_lookup(index_frame: pd.DataFrame | None):
    """date -> the market regime that stood on that date, point-in-time."""
    if index_frame is None or index_frame.empty:
        return None
    from src.strategy import regime

    labels = regime.regime_frame(index_frame)["label"]

    def at(day: date) -> str | None:
        value = labels.asof(pd.Timestamp(day))
        return None if value is None or (isinstance(value, float) and math.isnan(value)) else str(value)

    return at


# --- Running one rule -------------------------------------------------------------------


def rule_arrays(rule: swing.SwingRule, features: dict[str, pd.DataFrame], bars: dict[str, Bars]):
    if rule.cross_sectional:
        entries = swing.losers_entries(features)
    else:
        entries = {s: rule.entries(f) for s, f in features.items()}
    signals = {s: entries[s].to_numpy(dtype=bool) for s in bars}
    ranks = {s: rule.rank(features[s]).to_numpy(dtype=float) for s in bars}
    exits = {s: rule.exits(features[s]).to_numpy(dtype=bool) for s in bars}
    return signals, ranks, exits


def run_rule(
    rule: swing.SwingRule,
    plan: swing.ExitPlan,
    features: dict[str, pd.DataFrame],
    bars: dict[str, Bars],
    *,
    start: date,
    end: date,
    mode: str = NEXT_OPEN,
    max_positions: int | None = None,
    cfg: Any = None,
    regime_at=None,
    with_costs: bool = True,
    arrays=None,
) -> SimResult:
    signals, ranks, exits = arrays or rule_arrays(rule, features, bars)
    return simulate(
        bars, signals, ranks,
        lambda symbol: SwingPolicy(rule, plan, exits[symbol]),
        strategy=rule.key, start=start, end=end, mode=mode,
        position_value=POSITION_VALUE, max_positions=max_positions,
        initial_capital=POSITION_VALUE * MAX_SWING_POSITIONS if max_positions else None,
        first_tranche=rule.scale_in[0], cfg=cfg, regime_at=regime_at,
        with_costs=with_costs,
    )


def _brief(stats: dict[str, Any]) -> dict[str, Any]:
    keys = ("trades", "win_rate", "avg_win_pct", "avg_loss_pct", "profit_factor",
            "expectancy_pct", "expectancy_rs", "worst_pct", "avg_sessions", "avg_charges")
    return {k: stats.get(k) for k in keys}


def choose_plan(rule, features, bars, cfg, regime_at, end_train=TRAIN[1]):
    """Pick a stop/target plan using the training years only."""
    arrays = rule_arrays(rule, features, bars)
    table = []
    for plan in swing.plan_grid(rule):
        result = run_rule(rule, plan, features, bars, start=TRAIN[0], end=end_train,
                          cfg=cfg, regime_at=regime_at, arrays=arrays)
        stats = trade_stats(result.trades)
        table.append({"plan": plan, "stats": stats})

    enough = [r for r in table if r["stats"].get("trades", 0) >= PASS["min_train_trades"]]
    qualifying = [r for r in enough if r["stats"]["win_rate"] >= PASS["min_win_rate"]]
    pool = qualifying or enough or table
    best = max(pool, key=lambda r: (r["stats"].get("profit_factor", 0) or 0,
                                    r["stats"].get("win_rate", 0) or 0))
    return best["plan"], table, arrays


# --- The verdict -----------------------------------------------------------------------


@dataclass
class Verdict:
    rule: swing.SwingRule
    plan: swing.ExitPlan
    train: dict[str, Any]
    test: dict[str, Any]
    test_same_close: dict[str, Any]
    next50: dict[str, Any]
    portfolio: dict[str, Any]
    benchmark: dict[str, Any]
    grid: list[dict[str, Any]]
    passed: bool = False
    reasons: list[str] = field(default_factory=list)
    trades: list[Any] = field(default_factory=list)


def judge(v: Verdict) -> Verdict:
    reasons = []

    def check(ok: bool, text: str) -> None:
        if not ok:
            reasons.append(text)

    for label, stats, min_n in (("2014-21", v.train, PASS["min_train_trades"]),
                                ("2022+", v.test, PASS["min_test_trades"])):
        n = stats.get("trades", 0)
        check(n >= min_n, f"{label}: only {n} trades")
        if n:
            check(stats["win_rate"] >= PASS["min_win_rate"],
                  f"{label}: win rate {stats['win_rate']:.0%} below {PASS['min_win_rate']:.0%}")
            check(stats["profit_factor"] >= PASS["min_profit_factor"],
                  f"{label}: profit factor {stats['profit_factor']:.2f} below {PASS['min_profit_factor']}")
            check(stats["expectancy_pct"] > 0,
                  f"{label}: loses {stats['expectancy_pct']:.2f}% per trade on average")

    n50 = v.next50
    if n50.get("trades", 0):
        check(n50["win_rate"] >= PASS["next50_min_win_rate"],
              f"Nifty Next 50: win rate {n50['win_rate']:.0%} below {PASS['next50_min_win_rate']:.0%}")
        check(n50["profit_factor"] > PASS["next50_min_profit_factor"],
              f"Nifty Next 50: profit factor {n50['profit_factor']:.2f} not above {PASS['next50_min_profit_factor']}")
    else:
        reasons.append("Nifty Next 50: no trades")

    port_dd = v.portfolio.get("max_drawdown_pct", -100.0)
    bench_dd = v.benchmark.get("max_drawdown_pct", 0.0)
    check(port_dd >= bench_dd,
          f"portfolio drawdown {port_dd:.1f}% worse than holding the Nifty 50 ({bench_dd:.1f}%)")

    v.reasons = reasons
    v.passed = not reasons
    return v


def benchmark_curve(index_frame: pd.DataFrame | None, start: date, end: date, capital: float) -> pd.Series:
    if index_frame is None or index_frame.empty:
        return pd.Series(dtype=float)
    close = index_frame["close"]
    window = close[(close.index >= pd.Timestamp(start)) & (close.index <= pd.Timestamp(end))].dropna()
    if window.empty:
        return pd.Series(dtype=float)
    curve = window / float(window.iloc[0]) * capital
    curve.index = [d.date() for d in curve.index]
    return curve


def evaluate_rule(rule, nifty50, next50, *, end: date, cfg, regime_at, nifty_index) -> Verdict:
    """Steps 1-3 of the module docstring for one rule."""
    feats50, bars50 = nifty50
    featsN, barsN = next50

    plan, grid, arrays = choose_plan(rule, feats50, bars50, cfg, regime_at)
    train = run_rule(rule, plan, feats50, bars50, start=TRAIN[0], end=TRAIN[1],
                     cfg=cfg, regime_at=regime_at, arrays=arrays)
    test = run_rule(rule, plan, feats50, bars50, start=TEST_START, end=end,
                    cfg=cfg, regime_at=regime_at, arrays=arrays)
    test_close = run_rule(rule, plan, feats50, bars50, start=TEST_START, end=end,
                          mode=SAME_CLOSE, cfg=cfg, regime_at=regime_at, arrays=arrays)
    nxt = run_rule(rule, plan, featsN, barsN, start=TRAIN[0], end=end,
                   cfg=cfg, regime_at=regime_at)
    portfolio = run_rule(rule, plan, feats50, bars50, start=TEST_START, end=end,
                         max_positions=MAX_SWING_POSITIONS, cfg=cfg, regime_at=regime_at,
                         arrays=arrays)

    capital = POSITION_VALUE * MAX_SWING_POSITIONS
    port_stats = curve_stats(portfolio.equity, capital)
    port_stats["exposure"] = portfolio.exposure
    port_stats.update({f"trade_{k}": v for k, v in _brief(trade_stats(portfolio.trades)).items()})
    bench = curve_stats(benchmark_curve(nifty_index, TEST_START, end, capital), capital)

    verdict = Verdict(
        rule=rule, plan=plan,
        train=trade_stats(train.trades), test=trade_stats(test.trades),
        test_same_close=trade_stats(test_close.trades), next50=trade_stats(nxt.trades),
        portfolio=port_stats, benchmark=bench,
        grid=[{"plan": r["plan"].label, **_brief(r["stats"])} for r in grid],
        trades=[t.to_row() for t in train.trades + test.trades],
    )
    return judge(verdict)


def sanity_check_index(nifty_index: pd.DataFrame | None, end: date, cfg) -> dict[str, Any]:
    """RSI(2) on the Nifty 50 index before costs - the published direction.

    Published tests on broad indices find RSI(2) pullbacks winning well over
    60% of the time before costs. If this comes out wildly different, the
    simulator is suspect before the market is.
    """
    if nifty_index is None or nifty_index.empty:
        return {"trades": 0, "note": "index history unavailable"}
    feats = {"NIFTY50": swing.features(nifty_index)}
    bars = to_bars(feats)
    rule = swing.RULES["rsi2"]
    result = run_rule(rule, swing.ExitPlan(None, None, rule.max_hold), feats, bars,
                      start=TRAIN[0], end=end, cfg=cfg, with_costs=False)
    return _brief(trade_stats(result.trades))


# --- Long-term exits ----------------------------------------------------------------


def long_term_arrays(prices: dict[str, pd.DataFrame], cfg, *, every: int = 5):
    """Dip entries on the live rule layer, checked every `every` sessions."""
    from src import indicators as ind
    from src import screener
    from src.strategy.backtest import dip_signal_at

    frames, bars, signals, ranks = {}, {}, {}, {}
    for symbol, raw in prices.items():
        if raw is None or len(raw) < 400:
            continue
        adjusted = swing.adjust_for_dividends(raw)
        try:
            frame = ind.compute_indicator_frame(
                adjusted,
                rsi_period=int(cfg.get("dip.rsi_period", 14)),
                atr_period=int(cfg.get("dip.atr_period", 14)),
                dma_long=int(cfg.get("dip.dma_long", 200)),
                dma_short=int(cfg.get("dip.dma_short", 50)),
                slope_lookback=int(cfg.get("dip.dma_slope_lookback_days", 126)),
            )
        except Exception as exc:
            log.warning("Indicator frame failed for %s: %s", symbol, exc)
            continue
        frame["atr22"] = ind.atr(frame["high"], frame["low"], frame["close"], 22)
        frames[symbol] = frame

    calendar = sorted({d for f in frames.values() for d in f.index})
    checkpoints = set(calendar[::every])

    for symbol, frame in frames.items():
        n = len(frame)
        sig = np.zeros(n, dtype=bool)
        rank = np.full(n, np.nan)
        for i, day in enumerate(frame.index):
            if day not in checkpoints:
                continue
            fired, metrics = dip_signal_at(frame, i, cfg)
            if fired:
                sig[i] = True
                # Lower ranks are taken first, so the screen score is negated.
                rank[i] = -screener.score_candidate(metrics, cfg)
        signals[symbol] = sig
        ranks[symbol] = rank
        bars[symbol] = Bars.from_frame(symbol, frame, extra=("atr", "atr22"))
    return bars, signals, ranks


def run_long_term(plan: LongTermPlan, bars, signals, ranks, *, start: date, end: date,
                  cfg, regime_at=None, capital: float = 1_000_000.0,
                  position_pct: float = 8.0, max_positions: int = 15) -> SimResult:
    return simulate(
        bars, signals, ranks, lambda symbol: LongTermPolicy(plan),
        strategy=f"long_term_{plan.key}", start=start, end=end, mode=NEXT_OPEN,
        position_value=capital * position_pct / 100.0, max_positions=max_positions,
        initial_capital=capital, cfg=cfg, regime_at=regime_at,
    )


def choose_long_term(results: dict[str, dict[str, Any]]) -> tuple[str, str]:
    """Keep L1 unless another plan beats it on CAGR *and* drawdown out of sample."""
    base = results["L1"]["test_curve"]
    better = [
        key for key, r in results.items()
        if key != "L1"
        and r["test_curve"]["cagr_pct"] > base["cagr_pct"]
        and r["test_curve"]["max_drawdown_pct"] >= base["max_drawdown_pct"]
    ]
    if not better:
        return "L1", "No alternative beat the current plan on both growth and drawdown after 2022, so it stays."
    winner = max(better, key=lambda k: results[k]["test_curve"]["cagr_pct"])
    return winner, (
        f"{winner} beat the current plan on both growth and drawdown from 2022 onwards, "
        f"on positions it was not chosen on."
    )
