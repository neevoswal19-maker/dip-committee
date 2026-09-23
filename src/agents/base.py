"""The Analyst contract, and the seam an LLM would slot into.

Every bot splits in two:

    gather()  arithmetic over the evidence pack. Stays whatever happens.
    judge()   turns those metrics into a score. THE SEAM.

Today `judge` is a rule. Adding an LLM later means subclassing one bot and
overriding `judge` to send the same metrics to a model — no other file
changes. That granularity is the point: you can switch on just the five news
bots, where language actually helps, and measure whether they beat the rules
they replaced, instead of betting on the whole layer at once.

`run()` wraps both so a bot that raises, or finds no data, degrades into a
blind verdict rather than taking down a 23-bot committee.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from src.agents.schemas import Evidence, Verdict, clamp_score, stance_for

log = logging.getLogger(__name__)


class Analyst(ABC):
    """One bot on the org chart."""

    bot_id: str = "analyst"
    desk: str = "unassigned"
    name: str = "Analyst"
    role: str = "analyst"

    #: What this bot needs from the evidence pack. When every one of these is
    #: unavailable the bot reports blind without running, which keeps the
    #: "no data" path in one place rather than in every gather().
    requires: tuple[str, ...] = ()

    def __init__(self, cfg: Any = None):
        from src.config import load_config

        self.cfg = cfg or load_config()

    # --- The two halves -----------------------------------------------------

    @abstractmethod
    def gather(self, ctx: Any) -> dict[str, Any]:
        """Compute this bot's metrics from the evidence pack.

        Returns a plain dict. Must not raise for missing data - return
        `{"data_available": False, "note": ...}` instead.
        """

    @abstractmethod
    def judge(self, metrics: dict[str, Any]) -> Verdict:
        """Turn metrics into a scored verdict. The swappable half."""

    # --- The wrapper --------------------------------------------------------

    def run(self, ctx: Any) -> Verdict:
        # Gated on reachability, not on rows. A source that answered "nothing
        # was filed" gives a bot something true to report; only an outright
        # fetch failure should silence it.
        missing = [name for name in self.requires if not ctx.reachable(name)]
        if self.requires and len(missing) == len(self.requires):
            return Verdict.blind(
                self.bot_id, self.desk, self.name,
                f"needs {' or '.join(self.requires)}, none available",
                role=self.role,
            )

        try:
            metrics = self.gather(ctx)
        except Exception as exc:
            log.exception("%s failed while gathering", self.bot_id)
            return Verdict.blind(
                self.bot_id, self.desk, self.name, f"{type(exc).__name__}: {exc}", role=self.role
            )

        if not metrics or metrics.get("data_available") is False:
            return Verdict.blind(
                self.bot_id, self.desk, self.name,
                str(metrics.get("note", "no metrics")) if metrics else "no metrics",
                role=self.role,
            )

        try:
            verdict = self.judge(metrics)
        except Exception as exc:
            log.exception("%s failed while judging", self.bot_id)
            return Verdict.blind(
                self.bot_id, self.desk, self.name, f"{type(exc).__name__}: {exc}", role=self.role
            )

        verdict.score = clamp_score(verdict.score)
        verdict.confidence = max(0.0, min(1.0, verdict.confidence))
        if verdict.stance == "NEUTRAL":
            verdict.stance = stance_for(verdict.score)
        if missing:
            verdict.data_note = f"partial: missing {', '.join(missing)}"
            # Partial evidence should not carry full confidence.
            verdict.confidence *= 1.0 - (len(missing) / max(len(self.requires), 1)) * 0.5

        return verdict

    # --- Helpers for subclasses --------------------------------------------

    def verdict(
        self,
        score: float,
        confidence: float,
        findings: list[str] | None = None,
        evidence: list[Evidence] | None = None,
        red_flags: list[str] | None = None,
        *,
        veto: bool = False,
    ) -> Verdict:
        return Verdict(
            bot_id=self.bot_id, desk=self.desk, name=self.name, role=self.role,
            score=clamp_score(score),
            confidence=max(0.0, min(1.0, confidence)),
            stance=stance_for(clamp_score(score)),
            key_findings=findings or [],
            evidence=evidence or [],
            red_flags=red_flags or [],
            veto=veto,
        )


def scale(value: float, low: float, high: float, *, invert: bool = False) -> float:
    """Map a measurement onto -5..+5 between two reference points.

    `low` is where the reading is as bad as it gets, `high` as good. Values
    outside the range clamp rather than extrapolate, because a reading ten
    times better than expected almost always means a data error rather than a
    tenfold better company.
    """
    if high == low:
        return 0.0
    ratio = (float(value) - low) / (high - low)
    ratio = max(0.0, min(1.0, ratio))
    score = (ratio * 2.0 - 1.0) * 5.0
    return -score if invert else score


def band_score(value: float | None, bands: list[tuple[float, float]], default: float = 0.0) -> float:
    """Score by threshold bands: [(threshold, score), ...] highest first."""
    if value is None:
        return default
    for threshold, score in bands:
        if float(value) >= threshold:
            return score
    return bands[-1][1] if bands else default
