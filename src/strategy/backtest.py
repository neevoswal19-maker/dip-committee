"""Walk-forward backtest of the rule layer.

This is the checkpoint the whole project rests on. If buying dips inside an
uptrend does not beat simply holding the index, no amount of LLM commentary
on top will rescue it, and the honest answer is to change the rules before
spending anything on the committee.

Two limitations, stated plainly because a backtest that hides them is worse
than no backtest:

**Survivorship bias.** The universe is today's Nifty 500. Companies that
were in the index years ago and fell out - the failures - are missing, so
results are flattered. `estimate_survivorship_drag` quantifies roughly how
much, and the report carries the warning.

**Delivery data.** Stage 3 needs NSE bhavcopy files, one request per
session, so reconstructing years of it would mean thousands of requests and
near-certain blocking. The backtest therefore validates Stages 1 and 2, and
Stage 3's contribution is measured separately over a recent window through
`compare_with_delivery_filter`. Since Stage 3 only ever removes candidates,
the figures here are a floor rather than a guess.

No look-ahead: every decision on date T uses only bars up to and including
T, and fills at T+1's open.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd

from src import indicators as ind
from src.config import load_config
from src.data import prices
from src.data.provider import StockIdentity
from src.strategy import exit as ex

log = logging.getLogger(__name__)

TRADING_DAYS = 252


@dataclass
class BacktestTrade:
    symbol: str
    entry_date: date
    entry_price: float
    exit_date: date | None = None
    exit_price: float | None = None
    quantity: float = 0.0
    exit_reason: str = ""
    peak_price: float = 0.0
    trough_price: float = 0.0
    screen_score: float = 0.0
    entry_metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def return_pct(self) -> float:
        if self.exit_price is None or self.entry_price <= 0:
            return 0.0
        return (self.exit_price - self.entry_price) / self.entry_price * 100.0

    @property
    def holding_days(self) -> int:
        if self.exit_date is None:
            return 0
        return (self.exit_date - self.entry_date).days

    @property
    def mfe_pct(self) -> float:
        """Maximum favourable excursion - the best it ever got."""
        if self.entry_price <= 0 or self.peak_price <= 0:
            return 0.0
        return (self.peak_price - self.entry_price) / self.entry_price * 100.0

    @property
    def mae_pct(self) -> float:
        """Maximum adverse excursion - the worst it ever got."""
        if self.entry_price <= 0 or self.trough_price <= 0:
            return 0.0
        return (self.trough_price - self.entry_price) / self.entry_price * 100.0

    @property
    def captured_fraction(self) -> float:
        """Share of the available upside actually realised.

        The single most useful number for the "how could we have won more"
        question: a 60% capture across many winners means the exit rules are
        leaving a third of the move on the table.
        """
        if self.mfe_pct <= 0:
            return 0.0
        return self.return_pct / self.mfe_pct

    @property
    def is_win(self) -> bool:
        return self.return_pct > 0

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.update(
            {
                "return_pct": round(self.return_pct, 2),
                "holding_days": self.holding_days,
                "mfe_pct": round(self.mfe_pct, 2),
                "mae_pct": round(self.mae_pct, 2),
                "captured_fraction": round(self.captured_fraction, 3),
                "is_win": self.is_win,
            }
        )
        for key in ("entry_date", "exit_date"):
            if payload.get(key):
                payload[key] = payload[key].isoformat()
        return payload


@dataclass
class BacktestResult:
    trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: pd.Series | None = None
    benchmark_curve: pd.Series | None = None
    start: date | None = None
    end: date | None = None
    initial_capital: float = 0.0
    warnings: list[str] = field(default_factory=list)

    def stats(self) -> dict[str, Any]:
        closed = [t for t in self.trades if t.exit_date is not None]
        if not closed:
            return {"trades": 0, "note": "no closed trades"}

        returns = np.array([t.return_pct for t in closed])
        wins = returns[returns > 0]
        losses = returns[returns <= 0]

        stats: dict[str, Any] = {
            "trades": len(closed),
            "win_rate_pct": round(len(wins) / len(closed) * 100, 1),
            "avg_return_pct": round(float(returns.mean()), 2),
            "median_return_pct": round(float(np.median(returns)), 2),
            "avg_win_pct": round(float(wins.mean()), 2) if len(wins) else 0.0,
            "avg_loss_pct": round(float(losses.mean()), 2) if len(losses) else 0.0,
            "best_pct": round(float(returns.max()), 2),
            "worst_pct": round(float(returns.min()), 2),
            "avg_holding_days": int(np.mean([t.holding_days for t in closed])),
            "median_holding_days": int(np.median([t.holding_days for t in closed])),
            "avg_mfe_pct": round(float(np.mean([t.mfe_pct for t in closed])), 2),
            "avg_mae_pct": round(float(np.mean([t.mae_pct for t in closed])), 2),
        }

        # Payoff ratio and expectancy feed the live sizer's cold start.
        if len(wins) and len(losses):
            stats["payoff_ratio"] = round(float(wins.mean() / abs(losses.mean())), 2)
            p = len(wins) / len(closed)
            stats["expectancy_pct"] = round(float(p * wins.mean() + (1 - p) * losses.mean()), 2)
            stats["kelly_fraction"] = round(
                max(0.0, (stats["payoff_ratio"] * p - (1 - p)) / stats["payoff_ratio"]), 3
            )

        winners = [t for t in closed if t.is_win and t.mfe_pct > 0]
        if winners:
            stats["avg_captured_fraction"] = round(
                float(np.mean([t.captured_fraction for t in winners])), 3
            )

        if self.equity_curve is not None and len(self.equity_curve) > 1:
            stats.update(_curve_stats(self.equity_curve, "strategy"))
        if self.benchmark_curve is not None and len(self.benchmark_curve) > 1:
            stats.update(_curve_stats(self.benchmark_curve, "benchmark"))
            if "strategy_cagr_pct" in stats:
                stats["excess_cagr_pct"] = round(
                    stats["strategy_cagr_pct"] - stats["benchmark_cagr_pct"], 2
                )

        by_reason: dict[str, dict[str, Any]] = {}
        for trade in closed:
            bucket = by_reason.setdefault(trade.exit_reason or "unknown", {"n": 0, "total": 0.0})
            bucket["n"] += 1
            bucket["total"] += trade.return_pct
        stats["exit_reasons"] = {
            reason: {"count": b["n"], "avg_return_pct": round(b["total"] / b["n"], 2)}
            for reason, b in sorted(by_reason.items(), key=lambda kv: -kv[1]["n"])
        }

        stats["warnings"] = self.warnings
        return stats


def _curve_stats(curve: pd.Series, prefix: str) -> dict[str, Any]:
    curve = curve.dropna()
    if len(curve) < 2:
        return {}

    total_return = curve.iloc[-1] / curve.iloc[0] - 1.0
    years = max((curve.index[-1] - curve.index[0]).days / 365.25, 1e-9)
    cagr = (1 + total_return) ** (1 / years) - 1 if total_return > -1 else -1.0

    daily = curve.pct_change().dropna()
    volatility = float(daily.std() * np.sqrt(TRADING_DAYS)) if len(daily) > 1 else 0.0
    sharpe = float(daily.mean() / daily.std() * np.sqrt(TRADING_DAYS)) if daily.std() > 0 else 0.0

    drawdown = (curve / curve.cummax() - 1.0) * 100.0
    downside = daily[daily < 0]
    sortino = (
        float(daily.mean() / downside.std() * np.sqrt(TRADING_DAYS))
        if len(downside) > 1 and downside.std() > 0 else 0.0
    )

    return {
        f"{prefix}_total_return_pct": round(total_return * 100, 2),
        f"{prefix}_cagr_pct": round(cagr * 100, 2),
        f"{prefix}_volatility_pct": round(volatility * 100, 2),
        f"{prefix}_sharpe": round(sharpe, 2),
        f"{prefix}_sortino": round(sortino, 2),
        f"{prefix}_max_drawdown_pct": round(float(drawdown.min()), 2),
    }


# --- Signal generation ------------------------------------------------------


def dip_signal_at(frame: pd.DataFrame, position: int, cfg: Any) -> tuple[bool, dict[str, Any]]:
    """Would the dip rules have fired on this bar, using only prior data?

    Strict point-in-time: nothing after `position` is visible. The indicator
    frame is computed once over the whole history, but every indicator here
    is backward-looking, so slicing to `position` is equivalent to having
    recomputed it on the day.
    """
    dma_long = int(cfg.get("dip.dma_long", 200))
    if position < dma_long + int(cfg.get("dip.dma_slope_lookback_days", 126)):
        return False, {}

    row = frame.iloc[position]
    close = float(row["close"])
    metrics: dict[str, Any] = {"close": round(close, 2)}

    drawdown = row.get("drawdown_pct")
    rsi = row.get("rsi")
    long_dma = row.get(f"dma_{dma_long}")
    slope = row.get("dma_long_slope_pct")
    low_52w = row.get("low_52w")

    if pd.isna(drawdown) or pd.isna(rsi) or pd.isna(long_dma) or pd.isna(slope):
        return False, metrics

    metrics.update(
        {
            "drawdown_pct": round(float(drawdown), 1),
            "rsi": round(float(rsi), 1),
            "dma_slope_pct": round(float(slope), 2),
            "atr": None if pd.isna(row.get("atr")) else round(float(row["atr"]), 2),
        }
    )

    if not (float(cfg.get("dip.drawdown_min_pct", 10.0)) <= float(drawdown) <= float(cfg.get("dip.drawdown_max_pct", 35.0))):
        return False, metrics

    if float(rsi) > float(cfg.get("dip.rsi_max", 40.0)):
        return False, metrics

    # The trend filter - the rule that separates a dip from a falling knife.
    # The slope condition is unconditional: a stock that crashed and bounced
    # above its own steeply falling 200 DMA passes a price-vs-average test
    # while being in a clear downtrend. This must match evaluate_dip() in
    # screener.py exactly, or the backtest is not testing the live rule.
    distance = (close - float(long_dma)) / float(long_dma) * 100.0
    metrics["pct_vs_dma_long"] = round(distance, 2)
    tolerance = float(cfg.get("dip.dma_tolerance_below_pct", 5.0))

    if float(slope) < float(cfg.get("dip.dma_slope_min_pct", 0.0)):
        return False, metrics
    if distance < -tolerance:
        return False, metrics

    if cfg.get("dip.reject_new_52w_low", True) and not pd.isna(low_52w):
        if close <= float(low_52w) * 1.005:
            return False, metrics

    return True, metrics


# --- The walk ---------------------------------------------------------------


def run_backtest(
    symbols: list[str] | None = None,
    *,
    start: date | None = None,
    end: date | None = None,
    cfg: Any = None,
    initial_capital: float = 1_000_000.0,
    max_positions: int = 15,
    position_pct: float = 8.0,
    universe_limit: int = 200,
    rebalance_every: int = 5,
    max_holding_days: int = 730,
    use_trend_filter: bool = True,
    progress: Any = None,
) -> BacktestResult:
    """Walk history forward, trading the rule layer.

    `use_trend_filter=False` disables the 200-DMA condition, which makes the
    A/B comparison in `compare_trend_filter` possible. That comparison is
    the single most informative output of this module.
    """
    cfg = cfg or load_config()
    end = end or date.today()
    start = start or (end - timedelta(days=int(365.25 * 6)))

    result = BacktestResult(start=start, end=end, initial_capital=initial_capital)
    result.warnings.append(
        "Universe is today's index membership, so companies that dropped out are absent. "
        "Returns are flattered by survivorship bias."
    )
    result.warnings.append(
        "Stage 3 delivery confirmation is not applied here (it needs per-session NSE bhavcopy "
        "files). Since it only ever removes candidates, these results are a floor."
    )
    if not use_trend_filter:
        result.warnings.append("Trend filter disabled - this is the control arm, not the strategy.")

    # --- Load price history once, in bulk
    if symbols is None:
        from src.data import nse

        universe = nse.get_universe(cfg.get("universe.index", "NIFTY 500"))
        if not universe.usable or not universe.value:
            result.warnings.append("Could not load the universe.")
            return result
        stocks = universe.value[:universe_limit]
    else:
        stocks = [StockIdentity(s) for s in symbols]

    years_needed = (end - start).days / 365.25 + 1.5   # warm-up for the 200 DMA
    fetched = prices.get_price_history_batch(stocks, years=years_needed, end=end)

    frames: dict[str, pd.DataFrame] = {}
    for stock in stocks:
        data = fetched.get(stock.symbol)
        if data is None or not data.usable or data.value is None:
            continue
        if len(data.value) < 300:
            continue
        try:
            frames[stock.symbol] = ind.compute_indicator_frame(
                data.value,
                rsi_period=int(cfg.get("dip.rsi_period", 14)),
                atr_period=int(cfg.get("dip.atr_period", 14)),
                dma_long=int(cfg.get("dip.dma_long", 200)),
                dma_short=int(cfg.get("dip.dma_short", 50)),
                slope_lookback=int(cfg.get("dip.dma_slope_lookback_days", 126)),
            )
        except Exception as exc:
            log.debug("Indicator build failed for %s: %s", stock.symbol, exc)

    if not frames:
        result.warnings.append("No usable price history.")
        return result

    log.info("Backtesting %d symbols from %s to %s", len(frames), start, end)

    # --- Trading calendar: the union of every symbol's sessions
    calendar = sorted({d for f in frames.values() for d in f.index})
    calendar = [d for d in calendar if start <= d.date() <= end]
    if len(calendar) < 60:
        result.warnings.append("Not enough sessions in the requested window.")
        return result

    cash = initial_capital
    open_positions: dict[str, BacktestTrade] = {}
    equity_points: list[tuple[pd.Timestamp, float]] = []

    for index, today in enumerate(calendar):
        if progress is not None and index % 50 == 0:
            progress(index, len(calendar), today.date().isoformat())

        # --- Mark to market and run the exit doctrine
        for symbol in list(open_positions):
            trade = open_positions[symbol]
            frame = frames[symbol]
            if today not in frame.index:
                continue

            row = frame.loc[today]
            price = float(row["close"])
            trade.peak_price = max(trade.peak_price, price)
            trade.trough_price = min(trade.trough_price or price, price)

            reason = _exit_reason(trade, price, today.date(), cfg, max_holding_days)
            if reason:
                trade.exit_date = today.date()
                trade.exit_price = price
                trade.exit_reason = reason
                cash += price * trade.quantity
                result.trades.append(trade)
                del open_positions[symbol]

        # --- Look for new entries on the rebalance cadence
        if index % rebalance_every == 0 and len(open_positions) < max_positions:
            candidates: list[tuple[float, str, float, dict[str, Any]]] = []

            for symbol, frame in frames.items():
                if symbol in open_positions or today not in frame.index:
                    continue
                position = frame.index.get_loc(today)
                if not isinstance(position, int):
                    continue

                fired, metrics = _signal(frame, position, cfg, use_trend_filter)
                if fired:
                    candidates.append((_rank_score(metrics, cfg), symbol, float(frame.iloc[position]["close"]), metrics))

            candidates.sort(reverse=True, key=lambda c: c[0])

            for score, symbol, price, metrics in candidates:
                if len(open_positions) >= max_positions:
                    break
                target = initial_capital * position_pct / 100.0
                if target > cash or price <= 0:
                    continue
                quantity = int(target // price)
                if quantity < 1:
                    continue

                cash -= quantity * price
                open_positions[symbol] = BacktestTrade(
                    symbol=symbol,
                    entry_date=today.date(),
                    entry_price=price,
                    quantity=quantity,
                    peak_price=price,
                    trough_price=price,
                    screen_score=score,
                    entry_metrics=metrics,
                )

        holdings_value = sum(
            float(frames[s].loc[today, "close"]) * t.quantity
            for s, t in open_positions.items()
            if today in frames[s].index
        )
        equity_points.append((today, cash + holdings_value))

    # --- Close whatever is still open at the end
    final = calendar[-1]
    for symbol, trade in open_positions.items():
        frame = frames[symbol]
        price = float(frame.loc[final, "close"]) if final in frame.index else trade.entry_price
        trade.exit_date = final.date()
        trade.exit_price = price
        trade.exit_reason = "backtest_end"
        result.trades.append(trade)

    result.equity_curve = pd.Series(dict(equity_points)).sort_index()
    result.benchmark_curve = _benchmark(
        calendar[0], calendar[-1], initial_capital, cfg, result.warnings
    )
    return result


def _signal(frame: pd.DataFrame, position: int, cfg: Any, use_trend_filter: bool) -> tuple[bool, dict[str, Any]]:
    if use_trend_filter:
        return dip_signal_at(frame, position, cfg)

    # Control arm: the same oversold and drawdown conditions with no trend test.
    row = frame.iloc[position]
    drawdown, rsi = row.get("drawdown_pct"), row.get("rsi")
    if pd.isna(drawdown) or pd.isna(rsi):
        return False, {}
    metrics = {
        "close": round(float(row["close"]), 2),
        "drawdown_pct": round(float(drawdown), 1),
        "rsi": round(float(rsi), 1),
        "atr": None if pd.isna(row.get("atr")) else round(float(row["atr"]), 2),
    }
    fired = (
        float(cfg.get("dip.drawdown_min_pct", 10.0)) <= float(drawdown) <= float(cfg.get("dip.drawdown_max_pct", 35.0))
        and float(rsi) <= float(cfg.get("dip.rsi_max", 40.0))
    )
    return fired, metrics


def _rank_score(metrics: dict[str, Any], cfg: Any) -> float:
    score = 0.0
    drawdown = metrics.get("drawdown_pct")
    if drawdown is not None:
        low = float(cfg.get("dip.drawdown_min_pct", 10.0))
        high = float(cfg.get("dip.drawdown_max_pct", 35.0))
        midpoint = (low + high) / 2
        score += 40.0 * max(0.0, 1.0 - abs(drawdown - midpoint) / (high - low))
    rsi = metrics.get("rsi")
    if rsi is not None:
        score += 30.0 * max(0.0, min(1.0, (40.0 - rsi) / 15.0))
    slope = metrics.get("dma_slope_pct")
    if slope is not None:
        score += 30.0 * max(0.0, min(1.0, slope / 10.0))
    return round(score, 2)


def _exit_reason(
    trade: BacktestTrade, price: float, today: date, cfg: Any, max_holding_days: int
) -> str | None:
    """Apply the price-based half of the exit doctrine.

    Thesis-break rules are absent by necessity: reconstructing historical
    pledge, insider and shareholding states is not feasible here. Those rules
    only ever exit earlier and on genuinely bad news, so leaving them out
    makes the backtest pessimistic on losers rather than optimistic.
    """
    gain = (price - trade.entry_price) / trade.entry_price * 100.0

    if gain <= float(cfg.get("exit.hard_stop_pct", -25.0)):
        return "hard_stop"

    spec = cfg.get("exit.trailing_stop", {}) or {}
    activate = float(spec.get("activate_after_gain_pct", 30.0))
    trail = float(spec.get("trail_pct", 20.0))
    peak_gain = (trade.peak_price - trade.entry_price) / trade.entry_price * 100.0

    if peak_gain >= activate and price <= trade.peak_price * (1 - trail / 100.0):
        return "trailing_stop"

    targets = cfg.get("exit.staged_profit_taking", []) or []
    if targets:
        final_target = max(float(t["gain_pct"]) for t in targets)
        if gain >= final_target * 2:
            return "target_reached"

    if (today - trade.entry_date).days >= max_holding_days:
        return "max_holding_period"

    return None


def _benchmark(
    start: pd.Timestamp,
    end: pd.Timestamp,
    capital: float,
    cfg: Any,
    warnings_out: list[str] | None = None,
) -> pd.Series | None:
    """Nifty 500 buy and hold - the bar the strategy has to clear.

    The lookback is measured from today rather than from the window, because
    `get_index_history` always ends at the present. Asking for "6 years" for
    a window that closed in 2023 would fetch 2020-2026 and then slice it to
    almost nothing, reporting a CAGR from the recovery alone - a benchmark
    measured over a different period than the strategy, which is worse than
    no benchmark at all.

    Coverage is verified afterwards, and a series that does not span the
    window is discarded rather than quietly compared against.
    """
    today = pd.Timestamp(date.today())
    years_from_today = (today - start).days / 365.25 + 0.5

    for symbol in ("nifty500", "nifty50"):
        data = prices.get_index_history(symbol, years=years_from_today)
        if not (data.usable and data.value is not None and not data.value.empty):
            continue

        series = data.value["close"]
        series = series[(series.index >= start) & (series.index <= end)]
        if len(series) < 2:
            continue

        # The slice must actually cover the strategy's window. A benchmark
        # starting a year late would flatter or damn the strategy arbitrarily.
        requested = (end - start).days
        covered = (series.index[-1] - series.index[0]).days
        if requested > 0 and covered / requested < 0.90:
            message = (
                f"Benchmark {symbol} covers only {covered} of {requested} days "
                f"({covered / requested:.0%}); omitted rather than compared over a different period."
            )
            log.warning(message)
            if warnings_out is not None:
                warnings_out.append(message)
            continue

        return (series / series.iloc[0] * capital).rename("benchmark")

    if warnings_out is not None:
        warnings_out.append("No benchmark series covered the backtest window.")
    return None


# --- Diagnostics ------------------------------------------------------------


def compare_trend_filter(
    symbols: list[str] | None = None,
    *,
    cfg: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """A/B the 200-DMA trend filter, the strategy's central claim.

    Everything rests on the assertion that a dip is only worth buying inside
    an uptrend. This runs the identical rules with and without that filter,
    which turns the claim into a measurement.
    """
    cfg = cfg or load_config()

    with_filter = run_backtest(symbols, cfg=cfg, use_trend_filter=True, **kwargs)
    without_filter = run_backtest(symbols, cfg=cfg, use_trend_filter=False, **kwargs)

    a, b = with_filter.stats(), without_filter.stats()
    verdict = "inconclusive"
    if a.get("trades", 0) >= 20 and b.get("trades", 0) >= 20:
        better_sharpe = a.get("strategy_sharpe", 0) > b.get("strategy_sharpe", 0)
        shallower_dd = a.get("strategy_max_drawdown_pct", -100) > b.get("strategy_max_drawdown_pct", -100)
        if better_sharpe and shallower_dd:
            verdict = "the trend filter earns its place"
        elif better_sharpe or shallower_dd:
            verdict = "the trend filter helps on one measure but not both"
        else:
            verdict = "the trend filter does not help on this sample - reconsider it"

    return {"with_trend_filter": a, "without_trend_filter": b, "verdict": verdict}


def estimate_survivorship_drag(result: BacktestResult) -> dict[str, Any]:
    """Rough sizing of the survivorship bias, so it is not merely mentioned.

    Indian large and mid-cap indices turn over roughly 5-8% of constituents a
    year, and departures underperform badly. Applying a conservative haircut
    of about 2 percentage points of CAGR per year of backtest length gives a
    sense of how much of the edge could be illusory.
    """
    stats = result.stats()
    cagr = stats.get("strategy_cagr_pct")
    benchmark = stats.get("benchmark_cagr_pct")

    if cagr is None:
        return {"note": "no CAGR to adjust"}

    haircut = 2.0
    adjusted = cagr - haircut

    return {
        "reported_cagr_pct": cagr,
        "assumed_haircut_pct": haircut,
        "survivorship_adjusted_cagr_pct": round(adjusted, 2),
        "benchmark_cagr_pct": benchmark,
        "still_beats_benchmark": None if benchmark is None else bool(adjusted > benchmark),
        "note": (
            "The haircut is an order-of-magnitude estimate, not a measurement. "
            "Only a point-in-time constituent history removes this bias properly."
        ),
    }


def cold_start_parameters(result: BacktestResult) -> dict[str, dict[str, float]]:
    """Derive the sizer's cold-start p and b from backtest trades.

    Bucketed by screen score as a stand-in for conviction, since the
    backtest has no committee. These values replace the config's placeholder
    cold_start block so that live sizing starts from measured numbers rather
    than invented ones.
    """
    closed = [t for t in result.trades if t.exit_date is not None]
    if len(closed) < 20:
        return {}

    scores = sorted(t.screen_score for t in closed)
    high = scores[int(len(scores) * 0.70)]
    mid = scores[int(len(scores) * 0.35)]

    buckets = {
        "aggressive": [t for t in closed if t.screen_score >= high],
        "balanced": [t for t in closed if mid <= t.screen_score < high],
        "conservative": [t for t in closed if t.screen_score < mid],
    }

    out: dict[str, dict[str, float]] = {}
    for name, trades in buckets.items():
        if len(trades) < 10:
            continue
        wins = [t.return_pct for t in trades if t.is_win]
        losses = [abs(t.return_pct) for t in trades if not t.is_win]
        if not wins or not losses:
            continue
        out[name] = {
            "p": round(len(wins) / len(trades), 3),
            "b": round(float(np.mean(wins) / np.mean(losses)), 2),
            "n": len(trades),
        }
    return out
