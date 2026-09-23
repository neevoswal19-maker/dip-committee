"""The five desk heads.

A Lead does three things: weight its analysts by confidence, measure how much
they disagree, and record the dissent rather than averaging it away.

That last part matters. A desk where two analysts say +4 and two say -4
averages to zero, which looks identical to a desk where all four said nothing
much. They are completely different situations, and the CMIO needs to be able
to tell them apart - so `disagreement` travels alongside the score.
"""

from __future__ import annotations

import statistics
from typing import Any

from src.agents.schemas import DeskReport, Verdict, stance_for

DESK_NAMES = {
    "news": "Market News Research Lead",
    "equity": "Equity Research Lead",
    "macro": "Head of Industry & Geopolitical Research",
    "ownership": "Head of Market Activity & Ownership Research",
    "review": "Independent Research Review Lead",
}


def synthesise(desk: str, verdicts: list[Verdict]) -> DeskReport:
    """Aggregate one desk's analysts into a lead report."""
    report = DeskReport(desk=desk, name=DESK_NAMES.get(desk, desk.title()), verdicts=verdicts)

    if not verdicts:
        report.summary = "No analysts reported."
        report.coverage = 0.0
        return report

    working = [v for v in verdicts if v.data_available]
    report.coverage = len(working) / len(verdicts)

    if not working:
        report.stance = "NO_DATA"
        report.confidence = 0.0
        report.summary = (
            f"Every analyst on this desk was blind: "
            f"{', '.join(v.data_note or 'no data' for v in verdicts[:3])}. "
            f"The desk contributes nothing to the verdict rather than a neutral vote."
        )
        return report

    total_weight = sum(v.weight for v in working)
    if total_weight <= 0:
        report.summary = "Analysts reported but none carried any confidence."
        return report

    report.score = sum(v.weighted_score for v in working) / total_weight
    # Desk confidence is the mean analyst confidence, reduced when part of the
    # desk could not report at all.
    report.confidence = (sum(v.confidence for v in working) / len(working)) * report.coverage
    report.stance = stance_for(report.score)

    scores = [v.score for v in working]
    report.disagreement = statistics.pstdev(scores) if len(scores) > 1 else 0.0

    positive = [v for v in working if v.score > 0.5]
    negative = [v for v in working if v.score < -0.5]

    # Dissent is the minority view, recorded by name so the CMIO can say what
    # it overruled rather than silently averaging it out.
    if positive and negative:
        minority = negative if len(negative) <= len(positive) else positive
        for verdict in sorted(minority, key=lambda v: -abs(v.score))[:3]:
            headline = verdict.key_findings[0] if verdict.key_findings else verdict.stance
            report.dissent.append(f"{verdict.name} ({verdict.score:+.1f}): {headline}")

    report.summary = _summarise(report, positive, negative, working)
    return report


def _summarise(
    report: DeskReport,
    positive: list[Verdict],
    negative: list[Verdict],
    working: list[Verdict],
) -> str:
    parts = [
        f"{len(positive)} of {len(working)} analysts positive, {len(negative)} negative, "
        f"giving a desk score of {report.score:+.2f}."
    ]

    if report.disagreement > 2.0:
        parts.append(
            f"The desk is genuinely split (spread {report.disagreement:.1f}), so this score "
            f"is a compromise rather than a consensus."
        )
    elif report.disagreement < 0.8 and len(working) > 1:
        parts.append("The desk is close to unanimous.")

    if report.coverage < 1.0:
        blind = report.blind_analysts
        parts.append(
            f"{len(blind)} analyst(s) had no data ({', '.join(blind)}), so the desk's "
            f"confidence is reduced accordingly."
        )

    strongest = max(working, key=lambda v: abs(v.score) * max(v.confidence, 0.1))
    if strongest.key_findings:
        parts.append(f"Loudest signal - {strongest.name}: {strongest.key_findings[0]}")

    return " ".join(parts)


def as_verdict(report: DeskReport) -> Verdict:
    """Express a lead report in Verdict form, for uniform persistence."""
    return Verdict(
        bot_id=f"{report.desk}_lead",
        desk=report.desk,
        name=report.name,
        role="lead",
        score=report.score,
        confidence=report.confidence,
        stance=report.stance,
        key_findings=[report.summary] + report.dissent,
        red_flags=[flag for v in report.verdicts for flag in v.red_flags],
        data_available=report.coverage > 0,
        data_note=None if report.coverage == 1.0 else f"coverage {report.coverage:.0%}",
        veto=any(v.veto for v in report.verdicts),
    )
