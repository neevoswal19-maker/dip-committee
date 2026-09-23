"""The shapes every bot speaks in.

One uniform `Verdict` across all 23 bots is what makes the Leads' and the
CMIO's aggregation arithmetic rather than opinion, and it is what lets the
learning loop score each bot later by correlating its number against what
actually happened.

Two rules the whole design rests on:

**A claim must name its evidence.** Every finding carries the data field it
came from. The Research Validation Analyst checks this, and a bot that asserts
something it cannot point at loses confidence for it.

**Missing data is not neutral.** A bot that could not fetch its inputs reports
`data_available: False` and contributes zero weight to its desk, rather than
scoring 0 and quietly voting "fine". Absence of evidence widens the
uncertainty; it does not cast a vote.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, Literal

Stance = Literal["POSITIVE", "NEUTRAL", "NEGATIVE", "NO_DATA"]

#: Bot scores run -5 to +5. The bound matters: it makes scores comparable
#: across bots that measure entirely different things.
SCORE_MIN, SCORE_MAX = -5.0, 5.0


def clamp_score(value: float) -> float:
    return max(SCORE_MIN, min(SCORE_MAX, float(value)))


def stance_for(score: float, *, threshold: float = 1.0) -> Stance:
    if score >= threshold:
        return "POSITIVE"
    if score <= -threshold:
        return "NEGATIVE"
    return "NEUTRAL"


@dataclass
class Evidence:
    """One measurement, named so it can be traced back to its source."""

    field: str
    value: Any
    note: str | None = None

    def __str__(self) -> str:
        text = f"{self.field} = {self.value}"
        return f"{text} ({self.note})" if self.note else text


@dataclass
class Verdict:
    """What every bot returns, from a junior analyst to the CMIO."""

    bot_id: str
    desk: str
    name: str
    role: str = "analyst"          # analyst | lead | cmio

    score: float = 0.0             # -5 .. +5
    confidence: float = 0.0        # 0 .. 1
    stance: Stance = "NEUTRAL"

    key_findings: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    red_flags: list[str] = field(default_factory=list)

    data_available: bool = True
    data_note: str | None = None

    #: Only the Financial Forensics Analyst sets this. It overrides everything.
    veto: bool = False

    @property
    def weight(self) -> float:
        """How much this verdict counts toward its desk.

        Zero when the bot was blind, so a desk with two working bots out of
        four is weighted on those two rather than diluted by two neutral
        non-votes.
        """
        return self.confidence if self.data_available else 0.0

    @property
    def weighted_score(self) -> float:
        return self.score * self.weight

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["evidence"] = [asdict(e) for e in self.evidence]
        payload["weight"] = round(self.weight, 4)
        return payload

    @classmethod
    def blind(cls, bot_id: str, desk: str, name: str, note: str, role: str = "analyst") -> Verdict:
        """The verdict a bot returns when its data could not be fetched."""
        return cls(
            bot_id=bot_id, desk=desk, name=name, role=role,
            score=0.0, confidence=0.0, stance="NO_DATA",
            data_available=False, data_note=note,
            key_findings=[f"No usable data: {note}"],
        )


@dataclass
class DeskReport:
    """A Lead's synthesis of its analysts."""

    desk: str
    name: str
    verdicts: list[Verdict] = field(default_factory=list)

    score: float = 0.0
    confidence: float = 0.0
    stance: Stance = "NEUTRAL"
    summary: str = ""
    disagreement: float = 0.0      # spread of analyst scores, 0 = unanimous
    dissent: list[str] = field(default_factory=list)
    coverage: float = 1.0          # share of analysts that had data

    @property
    def blind_analysts(self) -> list[str]:
        return [v.name for v in self.verdicts if not v.data_available]

    def to_dict(self) -> dict[str, Any]:
        return {
            "desk": self.desk,
            "name": self.name,
            "score": round(self.score, 3),
            "confidence": round(self.confidence, 3),
            "stance": self.stance,
            "summary": self.summary,
            "disagreement": round(self.disagreement, 3),
            "dissent": self.dissent,
            "coverage": round(self.coverage, 3),
            "verdicts": [v.to_dict() for v in self.verdicts],
        }


@dataclass
class CommitteeReport:
    """The CMIO's final output: what the whole committee concluded."""

    symbol: str
    run_at: datetime = field(default_factory=datetime.now)
    trade_date: date | None = None
    price: float = 0.0
    sector: str | None = None

    desks: list[DeskReport] = field(default_factory=list)

    conviction: float = 50.0       # 0 .. 100
    stance: str = "WATCH"          # BUY | WATCH | NO_BUY
    weighted_score: float = 0.0
    confidence: float = 0.0
    coverage: float = 1.0

    forensics_veto: bool = False
    bull_bear_agree: bool = True
    red_flags: list[str] = field(default_factory=list)

    summary: str = ""
    dissent: str = ""
    blind_desks: list[str] = field(default_factory=list)

    #: The broad market's regime on the run date - see strategy/regime.py.
    market_regime: dict[str, Any] | None = None

    #: Filled by the CMIO from the existing sizing and exit modules.
    sizing: dict[str, Any] | None = None
    exit_doctrine: dict[str, Any] | None = None

    duration_seconds: float = 0.0

    @property
    def all_verdicts(self) -> list[Verdict]:
        return [v for desk in self.desks for v in desk.verdicts]

    def verdict_for(self, bot_id: str) -> Verdict | None:
        return next((v for v in self.all_verdicts if v.bot_id == bot_id), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "run_at": self.run_at.isoformat(),
            "trade_date": self.trade_date.isoformat() if self.trade_date else None,
            "price": self.price,
            "sector": self.sector,
            "conviction": round(self.conviction, 1),
            "stance": self.stance,
            "weighted_score": round(self.weighted_score, 3),
            "confidence": round(self.confidence, 3),
            "coverage": round(self.coverage, 3),
            "forensics_veto": self.forensics_veto,
            "bull_bear_agree": self.bull_bear_agree,
            "red_flags": self.red_flags,
            "summary": self.summary,
            "dissent": self.dissent,
            "blind_desks": self.blind_desks,
            "sizing": self.sizing,
            "exit_doctrine": self.exit_doctrine,
            "desks": [d.to_dict() for d in self.desks],
            "duration_seconds": round(self.duration_seconds, 2),
        }


def score_to_conviction(weighted_score: float) -> float:
    """Map a -5..+5 committee score onto 0..100.

    Linear and deliberately boring: +5 is 100, 0 is 50, -5 is 0. A cleverer
    curve would make the number harder to reason about, and conviction feeds
    straight into position sizing where the mapping needs to stay legible.
    """
    return max(0.0, min(100.0, 50.0 + (clamp_score(weighted_score) / SCORE_MAX) * 50.0))
