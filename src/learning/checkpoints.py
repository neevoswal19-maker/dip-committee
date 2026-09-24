"""What happened after each recommendation.

The learning loop cannot wait for trades to close - a long-term position may
be held for a year or more, and the owner takes only some of what the
committee recommends. So every committee run is followed at fixed horizons
whether or not anything was bought: the stock's return from the run's price,
the Nifty 500's return over the same days, and the difference.

That difference - the excess return - is what the bots are scored against.
A bot that was positive before stocks that went on to beat the index is
useful; one that was positive before laggards is not.

Horizons are calendar days. Long-term runs are followed at 30, 90, 180 and
365 days (config: learning.checkpoint_days); swing runs at 7 and 14.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Callable

import pandas as pd
from sqlalchemy import select

from src import db

log = logging.getLogger(__name__)

SWING_HORIZONS = (7, 14)


def horizons_for(strategy: str | None, cfg: Any) -> tuple[int, ...]:
    if strategy == "swing":
        return SWING_HORIZONS
    return tuple(int(h) for h in (cfg.get("learning.checkpoint_days", [30, 90, 180, 365]) or []))


def _close_on_or_after(closes: pd.Series, day: date) -> tuple[date, float] | None:
    after = closes[closes.index >= pd.Timestamp(day)].dropna()
    if after.empty:
        return None
    return after.index[0].date(), float(after.iloc[0])


def _close_on_or_before(closes: pd.Series, day: date) -> float | None:
    before = closes[closes.index <= pd.Timestamp(day)].dropna()
    return None if before.empty else float(before.iloc[-1])


def measure(
    start: date,
    entry_price: float,
    horizon_days: int,
    closes: pd.Series,
    benchmark: pd.Series | None,
    today: date,
) -> dict[str, Any] | None:
    """Forward and excess return `horizon_days` after `start`, or None if not yet due.

    The end point is the first close on or after the horizon date, and only
    if that date has passed - a horizon that ends tomorrow is not measured
    today on a partial answer.
    """
    target = start + timedelta(days=horizon_days)
    if target > today or entry_price <= 0:
        return None
    end = _close_on_or_after(closes, target)
    if end is None:
        return None
    end_day, end_price = end
    forward = (end_price / entry_price - 1.0) * 100.0

    bench = None
    if benchmark is not None and not benchmark.empty:
        b_start = _close_on_or_before(benchmark, start)
        b_end = _close_on_or_after(benchmark, target)
        if b_start and b_end:
            bench = (b_end[1] / b_start - 1.0) * 100.0

    return {
        "measured_at": end_day,
        "forward_return_pct": round(forward, 4),
        "benchmark_return_pct": None if bench is None else round(bench, 4),
        "excess_return_pct": None if bench is None else round(forward - bench, 4),
    }


def _default_closes(symbol: str) -> pd.Series | None:
    from src.data import prices
    from src.data.provider import StockIdentity

    result = prices.get_price_history(StockIdentity(symbol), years=2)
    if not result.usable or result.value is None or result.value.empty:
        return None
    return result.value["close"]


def _default_benchmark() -> pd.Series | None:
    from src.data import prices

    result = prices.get_index_history("nifty500", years=2)
    if not result.usable or result.value is None or result.value.empty:
        return None
    return result.value["close"]


def update(
    cfg: Any,
    *,
    today: date | None = None,
    closes_for: Callable[[str], pd.Series | None] = _default_closes,
    benchmark_closes: Callable[[], pd.Series | None] = _default_benchmark,
) -> int:
    """Measure every horizon that has come due and is not yet recorded."""
    today = today or db.now().date()

    with db.connection() as conn:
        runs = conn.execute(
            select(db.committee_runs.c.id, db.committee_runs.c.symbol,
                   db.committee_runs.c.run_at, db.committee_runs.c.trade_date,
                   db.committee_runs.c.price_at_run, db.committee_runs.c.strategy)
        ).fetchall()
        done = {
            (r.committee_run_id, r.horizon_days)
            for r in conn.execute(select(db.trade_checkpoints.c.committee_run_id,
                                         db.trade_checkpoints.c.horizon_days))
        }

    due: list[tuple[Any, int, date]] = []
    for run in runs:
        start = run.trade_date or (run.run_at.date() if run.run_at else None)
        if start is None or not run.price_at_run:
            continue
        for horizon in horizons_for(run.strategy, cfg):
            if (run.id, horizon) not in done and start + timedelta(days=horizon) <= today:
                due.append((run, horizon, start))

    if not due:
        return 0

    benchmark = benchmark_closes()
    cache: dict[str, pd.Series | None] = {}
    rows = []
    for run, horizon, start in due:
        if run.symbol not in cache:
            try:
                cache[run.symbol] = closes_for(run.symbol)
            except Exception as exc:
                log.warning("No prices for %s: %s", run.symbol, exc)
                cache[run.symbol] = None
        closes = cache[run.symbol]
        if closes is None:
            continue
        result = measure(start, float(run.price_at_run), horizon, closes, benchmark, today)
        if result:
            rows.append({"committee_run_id": run.id, "symbol": run.symbol,
                         "horizon_days": horizon, **result})

    if rows:
        with db.connection() as conn:
            conn.execute(db.trade_checkpoints.insert().values(rows))
    log.info("Checkpoints: %d measured, %d due", len(rows), len(due))
    return len(rows)


def maturity(cfg: Any) -> dict[int, int]:
    """How many runs have been measured at each horizon - for the dashboard."""
    counts: dict[int, int] = {h: 0 for h in horizons_for("long_term", cfg)}
    with db.connection() as conn:
        for row in conn.execute(select(db.trade_checkpoints.c.horizon_days)):
            counts[row.horizon_days] = counts.get(row.horizon_days, 0) + 1
    return counts
