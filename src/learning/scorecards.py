"""How good is each bot, measured against what actually happened.

Every analyst and every desk lead scored each stock before its outcome was
known; `checkpoints` later records how the stock did against the index. The
information coefficient between the two - Spearman rank correlation - says
whether the bot's higher scores went with better outcomes.

Verdicts, in order of precedence:

  insufficient  fewer than `learning.min_observations` scored outcomes
  noise         p > 0.10, or |IC| below the 0.03 noise floor
  predictive    a real, positive relationship
  misleading    a real relationship in the wrong direction

A blind bot - one that had no data for a stock - is left out of that stock's
comparison rather than counted as a zero, matching how the committee treats
it when voting.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from src import db
from src.learning import attribution

log = logging.getLogger(__name__)

NOISE_P = 0.10


@dataclass
class Scorecard:
    bot_id: str
    desk: str
    role: str
    horizon_days: int
    n: int
    ic: float | None
    p_value: float | None
    hit_rate: float | None
    avg_score_on_wins: float | None
    avg_score_on_losses: float | None
    verdict: str


def verdict_for(n: int, ic: float | None, p: float | None, min_n: int) -> str:
    if n < min_n or ic is None:
        return "insufficient"
    if p is None or p > NOISE_P or abs(ic) < attribution.NOISE_FLOOR:
        return "noise"
    return "predictive" if ic > 0 else "misleading"


def observations(horizon: int) -> list[dict[str, Any]]:
    """(bot, score, outcome) for every scored verdict whose horizon has matured."""
    with db.connection() as conn:
        rows = conn.execute(
            select(
                db.bot_verdicts.c.run_id, db.bot_verdicts.c.bot_id, db.bot_verdicts.c.desk,
                db.bot_verdicts.c.role, db.bot_verdicts.c.score,
                db.trade_checkpoints.c.excess_return_pct, db.committee_runs.c.run_at,
            )
            .join(db.trade_checkpoints, db.trade_checkpoints.c.committee_run_id == db.bot_verdicts.c.run_id)
            .join(db.committee_runs, db.committee_runs.c.id == db.bot_verdicts.c.run_id)
            .where(db.trade_checkpoints.c.horizon_days == horizon)
            .where(db.bot_verdicts.c.data_available.is_(True))
            .where(db.trade_checkpoints.c.excess_return_pct.isnot(None))
        ).fetchall()
    return [dict(r._mapping) for r in rows]


def score(obs: list[dict[str, Any]], horizon: int, min_n: int) -> list[Scorecard]:
    by_bot: dict[str, list[dict[str, Any]]] = {}
    for o in obs:
        if o["role"] == "cmio" or o["score"] is None:
            continue
        by_bot.setdefault(o["bot_id"], []).append(o)

    cards = []
    for bot_id, rows in sorted(by_bot.items()):
        scores = [float(r["score"]) for r in rows]
        outcomes = [float(r["excess_return_pct"]) for r in rows]
        n = len(rows)
        ic = p = None
        if n >= 10 and len(set(scores)) >= 3:
            result = attribution.information_coefficient(scores, outcomes, name=bot_id, horizon_days=horizon)
            if result is not None:
                ic, p = result.ic, result.p_value

        signed = [(s, o) for s, o in zip(scores, outcomes) if s != 0]
        hit = sum(1 for s, o in signed if (s > 0) == (o > 0)) / len(signed) if signed else None
        wins = [s for s, o in zip(scores, outcomes) if o > 0]
        losses = [s for s, o in zip(scores, outcomes) if o <= 0]

        cards.append(Scorecard(
            bot_id=bot_id, desk=rows[0]["desk"], role=rows[0]["role"], horizon_days=horizon, n=n,
            ic=ic, p_value=p, hit_rate=hit,
            avg_score_on_wins=sum(wins) / len(wins) if wins else None,
            avg_score_on_losses=sum(losses) / len(losses) if losses else None,
            verdict=verdict_for(n, ic, p, min_n),
        ))
    return cards


def compute(cfg: Any, *, write: bool = True) -> list[Scorecard]:
    """Score every bot at every horizon, and record the result."""
    min_n = int(cfg.get("learning.min_observations", 30))
    horizons = sorted({int(h) for h in (cfg.get("learning.checkpoint_days", [30, 90, 180, 365]) or [])})
    cards: list[Scorecard] = []
    for horizon in horizons:
        cards.extend(score(observations(horizon), horizon, min_n))

    if write and cards:
        now = db.now()
        with db.connection() as conn:
            conn.execute(db.bot_attribution.insert().values([
                {
                    "computed_at": now, "bot_id": c.bot_id, "desk": c.desk,
                    "horizon_days": c.horizon_days, "n_observations": c.n,
                    "information_coefficient": c.ic, "ic_p_value": c.p_value,
                    "hit_rate": c.hit_rate, "avg_score_on_wins": c.avg_score_on_wins,
                    "avg_score_on_losses": c.avg_score_on_losses, "verdict": c.verdict,
                }
                for c in cards
            ]))
    return cards


def latest(limit_per_bot: int = 1) -> list[dict[str, Any]]:
    """The most recent scorecard for each bot and horizon, for display."""
    with db.connection() as conn:
        rows = conn.execute(
            select(db.bot_attribution).order_by(db.bot_attribution.c.id.desc())
        ).fetchall()
    seen: set[tuple[str, int]] = set()
    out = []
    for row in rows:
        key = (row.bot_id, row.horizon_days)
        if key in seen:
            continue
        seen.add(key)
        out.append(dict(row._mapping))
    return out
