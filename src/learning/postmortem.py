"""A review of every closed trade, wins included.

A loss asks "what did we miss?" and a win asks "how could we have made more?"
Both are answered from the trade's own record and the stock's real prices:

* **Capture** - how much of the best price reached was actually banked. A
  winner sold at +12% on a stock that went to +30% left most of the move.
* **Alternatives** - what three other exits would have made on the same
  prices: holding 30 days longer, and trailing stops of 10% and 20% below
  the peak close. These feed `proposals`, which only ever suggests.
* **The committee** - which desks leaned the right way at entry, and which
  the wrong way.

Each review is one trade, so its lessons carry low confidence (0.3). Single
trades teach anecdotes; the pattern across many is what `proposals` looks for.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable

import pandas as pd
from sqlalchemy import select

from src import db

log = logging.getLogger(__name__)

SINGLE_TRADE_CONFIDENCE = 0.3
#: Points of the peak move left unbanked before a winner earns an exit lesson.
LEFT_ON_TABLE_POINTS = 5.0
#: Days of price after the exit that alternatives may use.
AFTER_EXIT_DAYS = 60


@dataclass
class Review:
    trade_id: int
    symbol: str
    outcome: str                       # win | loss
    return_pct: float
    mfe_pct: float
    captured: float | None
    left_on_table: float
    exit_reason: str
    holding_days: int
    alternatives: dict[str, float] = field(default_factory=dict)
    desks_right: list[str] = field(default_factory=list)
    desks_wrong: list[str] = field(default_factory=list)
    regime: str | None = None
    lessons: list[tuple[str, str]] = field(default_factory=list)   # (category, text)


def trailing_exit(closes: pd.Series, entry: float, trail_pct: float) -> float | None:
    """Return from `entry` if sold on the first close `trail_pct` below the peak close."""
    if closes.empty or entry <= 0:
        return None
    peak = entry
    for price in closes:
        peak = max(peak, float(price))
        if price <= peak * (1 - trail_pct / 100.0):
            return (float(price) / entry - 1.0) * 100.0
    return (float(closes.iloc[-1]) / entry - 1.0) * 100.0


def alternatives(closes: pd.Series, entry_date: date, exit_date: date, entry: float) -> dict[str, float]:
    """What other exits would have made, on the stock's actual prices."""
    window = closes[(closes.index >= pd.Timestamp(entry_date)) &
                    (closes.index <= pd.Timestamp(exit_date + timedelta(days=AFTER_EXIT_DAYS)))].dropna()
    out: dict[str, float] = {}
    if window.empty or entry <= 0:
        return out

    later = window[window.index >= pd.Timestamp(exit_date + timedelta(days=30))]
    if not later.empty:
        out["held 30 more days"] = round((float(later.iloc[0]) / entry - 1.0) * 100.0, 2)
    for trail in (10.0, 20.0):
        value = trailing_exit(window, entry, trail)
        if value is not None:
            out[f"{trail:g}% trailing stop"] = round(value, 2)
    return out


def review(trade: dict[str, Any], closes: pd.Series | None, desk_scores: dict[str, float],
           regime: str | None = None) -> Review:
    ret = float(trade.get("return_pct") or 0.0)
    mfe = float(trade.get("mfe_pct") or 0.0)
    entry = float(trade.get("entry_price") or 0.0)
    entry_date, exit_date = trade.get("entry_date"), trade.get("exit_date")
    outcome = "win" if ret > 0 else "loss"
    left = max(0.0, mfe - ret)

    rv = Review(
        trade_id=int(trade["id"]), symbol=trade["symbol"], outcome=outcome, return_pct=ret,
        mfe_pct=mfe, captured=(ret / mfe) if mfe > 0 else None, left_on_table=left,
        exit_reason=trade.get("exit_reason") or "closed",
        holding_days=int(trade.get("holding_days") or 0), regime=regime,
    )
    if closes is not None and entry_date and exit_date:
        rv.alternatives = alternatives(closes, entry_date, exit_date, entry)

    for desk, s in sorted(desk_scores.items()):
        if s == 0:
            continue
        (rv.desks_right if (s > 0) == (ret > 0) else rv.desks_wrong).append(desk)

    best = max(rv.alternatives.items(), key=lambda kv: kv[1], default=None)
    if outcome == "win":
        if left >= LEFT_ON_TABLE_POINTS:
            text = (f"Sold {trade['symbol']} at {ret:+.1f}% while it reached {mfe:+.1f}% - "
                    f"{left:.1f} points left on the table.")
            if best and best[1] > ret:
                text += f" A {best[0]} would have made {best[1]:+.1f}%."
            rv.lessons.append(("exit", text))
        else:
            rv.lessons.append(("exit", f"{trade['symbol']} captured {ret:+.1f}% of a {mfe:+.1f}% peak - "
                                       f"the exit worked."))
    else:
        text = f"{trade['symbol']} lost {ret:.1f}% ({rv.exit_reason.replace('_', ' ')})."
        if mfe >= 5.0:
            text += f" It was up {mfe:+.1f}% first - a gain that turned into a loss."
            category = "exit"
        else:
            category = "thesis_break" if rv.exit_reason in ("stop", "hard_stop", "thesis_break") else "timing"
        if best and best[1] > ret:
            text += f" A {best[0]} would have made {best[1]:+.1f}%."
        rv.lessons.append((category, text))

    if rv.desks_right or rv.desks_wrong:
        rv.lessons.append(("attribution",
                           f"Right at entry: {', '.join(rv.desks_right) or 'none'}. "
                           f"Wrong: {', '.join(rv.desks_wrong) or 'none'}."))
    return rv


def _desk_scores(committee_run_id: int | None) -> dict[str, float]:
    if not committee_run_id:
        return {}
    with db.connection() as conn:
        rows = conn.execute(
            select(db.bot_verdicts.c.desk, db.bot_verdicts.c.score)
            .where(db.bot_verdicts.c.run_id == committee_run_id)
            .where(db.bot_verdicts.c.role == "lead")
        ).fetchall()
    return {r.desk: float(r.score) for r in rows if r.score is not None}


def _regime(committee_run_id: int | None) -> str | None:
    if not committee_run_id:
        return None
    with db.connection() as conn:
        row = conn.execute(select(db.committee_runs.c.sizing_json)
                           .where(db.committee_runs.c.id == committee_run_id)).first()
    sizing = db.from_json(row.sizing_json, {}) if row else {}
    return (sizing.get("market_regime") or {}).get("label")


def unreviewed_trades() -> list[dict[str, Any]]:
    with db.connection() as conn:
        reviewed = {r.trade_id for r in conn.execute(select(db.lessons.c.trade_id)) if r.trade_id}
        rows = conn.execute(select(db.trades).order_by(db.trades.c.id)).fetchall()
    return [dict(r._mapping) for r in rows if r.id not in reviewed]


def run(cfg: Any, *, closes_for: Callable[[str], pd.Series | None] | None = None,
        write: bool = True) -> list[Review]:
    """Review every closed trade that has not been reviewed yet."""
    from src.learning.checkpoints import _default_closes

    loader = closes_for or _default_closes
    reviews = []
    for trade in unreviewed_trades():
        try:
            closes = loader(trade["symbol"])
        except Exception as exc:
            log.warning("No prices for %s: %s", trade["symbol"], exc)
            closes = None
        rv = review(trade, closes, _desk_scores(trade.get("committee_run_id")),
                    _regime(trade.get("committee_run_id")))
        reviews.append(rv)
        if write:
            evidence = db.to_json({
                "return_pct": rv.return_pct, "mfe_pct": rv.mfe_pct, "captured": rv.captured,
                "left_on_table": rv.left_on_table, "alternatives": rv.alternatives,
                "desks_right": rv.desks_right, "desks_wrong": rv.desks_wrong,
                "regime": rv.regime, "exit_reason": rv.exit_reason,
            })
            with db.connection() as conn:
                conn.execute(db.lessons.insert().values([
                    {"created_at": db.now(), "trade_id": rv.trade_id, "symbol": rv.symbol,
                     "outcome": rv.outcome, "category": category, "lesson": text,
                     "evidence": evidence, "confidence": SINGLE_TRADE_CONFIDENCE}
                    for category, text in rv.lessons
                ]))
    return reviews


def message(rv: Review) -> str:
    """The Telegram trade review."""
    from src.alerts.telegram import _escape

    lines = [f"<b>Trade review: {_escape(rv.symbol)} ({rv.outcome.upper()}, {rv.return_pct:+.1f}%)</b>",
             f"Held {rv.holding_days} days; best point {rv.mfe_pct:+.1f}%; exit: {_escape(rv.exit_reason.replace('_', ' '))}."]
    if rv.alternatives:
        lines.append("Other exits on the same prices: " + ", ".join(
            f"{_escape(k)} {v:+.1f}%" for k, v in rv.alternatives.items()))
    lines.append("")
    lines.extend(f"- {_escape(text)}" for _, text in rv.lessons)
    lines.extend(["", "<i>One trade is an anecdote. Suggestions come only from patterns across many.</i>"])
    return "\n".join(lines)
