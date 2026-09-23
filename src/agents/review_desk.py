"""Independent Research Review Lead - three analysts.

These three are different from the rest: they read the other bots' verdicts
rather than raw data. That is why the orchestrator runs them last, with the
first fourteen verdicts already in the context.

Bull and Bear are adversarial by construction, not by instruction. Each is
only allowed to see evidence supporting its own side, so neither can hedge.
A rule implementation cannot argue, but it can be made to *select* honestly,
and the discipline of forcing each side to state its best case separately is
most of what the exercise is for. If the two ever agree, something is wrong -
there is a test asserting they do not.
"""

from __future__ import annotations

from typing import Any

from src.agents.base import Analyst
from src.agents.schemas import Evidence, Verdict

DESK = "review"


def _peer_verdicts(ctx: Any) -> list[Verdict]:
    return [v for v in ctx.peer_verdicts.values() if isinstance(v, Verdict)]


class BullCaseAnalyst(Analyst):
    """The strongest case for buying, built only from supporting evidence."""

    bot_id = "bull_case_analyst"
    desk = DESK
    name = "Bull Case Analyst"

    def gather(self, ctx: Any) -> dict[str, Any]:
        verdicts = _peer_verdicts(ctx)
        if not verdicts:
            return {"data_available": False, "note": "no analyst verdicts to argue from"}

        supporting = [v for v in verdicts if v.data_available and v.score > 0.5]
        strongest = sorted(supporting, key=lambda v: -(v.score * max(v.confidence, 0.1)))

        return {
            "total_analysts": len([v for v in verdicts if v.data_available]),
            "supporting": len(supporting),
            "strongest": strongest[:5],
            "conviction_sum": sum(v.score * v.confidence for v in supporting),
            "desks_supporting": len({v.desk for v in supporting}),
        }

    def judge(self, metrics: dict[str, Any]) -> Verdict:
        strongest = metrics["strongest"]
        supporting = metrics["supporting"]
        total = metrics["total_analysts"]

        if not strongest:
            return self.verdict(
                -1.0, 0.5,
                ["No analyst found positive evidence. There is no bull case to make here."],
                [Evidence("supporting_analysts", 0)],
            )

        findings = [
            f"{supporting} of {total} analysts found supporting evidence, across "
            f"{metrics['desks_supporting']} desks."
        ]
        for verdict in strongest:
            headline = verdict.key_findings[0] if verdict.key_findings else verdict.stance
            findings.append(f"{verdict.name} ({verdict.score:+.1f}): {headline}")

        if metrics["desks_supporting"] >= 3:
            findings.append(
                "The case does not rest on one desk - independent lines of evidence agree, "
                "which is worth more than several bots reading the same number."
            )
        elif metrics["desks_supporting"] == 1:
            findings.append(
                "All supporting evidence comes from a single desk. That is a narrow case, "
                "however strong it reads."
            )

        breadth = supporting / max(total, 1)
        score = min(5.0, metrics["conviction_sum"] / max(supporting, 1) * 1.3 + breadth * 1.5)
        confidence = min(0.8, 0.3 + metrics["desks_supporting"] * 0.12)

        return self.verdict(score, confidence, findings,
                            [Evidence("supporting_analysts", f"{supporting} of {total}"),
                             Evidence("desks_supporting", metrics["desks_supporting"])])


class BearCaseAnalyst(Analyst):
    """The strongest case against, built only from opposing evidence."""

    bot_id = "bear_case_analyst"
    desk = DESK
    name = "Bear Case Analyst"

    def gather(self, ctx: Any) -> dict[str, Any]:
        verdicts = _peer_verdicts(ctx)
        if not verdicts:
            return {"data_available": False, "note": "no analyst verdicts to argue from"}

        available = [v for v in verdicts if v.data_available]
        opposing = [v for v in available if v.score < -0.5]
        flags = [(v.name, flag) for v in verdicts for flag in v.red_flags]
        vetoes = [v.name for v in verdicts if v.veto]

        return {
            "total_analysts": len(available),
            "opposing": len(opposing),
            "strongest": sorted(opposing, key=lambda v: v.score * max(v.confidence, 0.1))[:5],
            "conviction_sum": sum(v.score * v.confidence for v in opposing),
            "red_flags": flags,
            "vetoes": vetoes,
            "blind_count": len([v for v in verdicts if not v.data_available]),
        }

    def judge(self, metrics: dict[str, Any]) -> Verdict:
        strongest = metrics["strongest"]
        flags = metrics["red_flags"]
        findings: list[str] = []

        if metrics["vetoes"]:
            findings.append(
                f"{', '.join(metrics['vetoes'])} raised a veto. That alone should end the discussion."
            )

        if not strongest and not flags:
            findings.append(
                "No analyst found opposing evidence and no red flags were raised. The honest "
                "bear case is thin - which is itself worth stating plainly rather than "
                "manufacturing a concern."
            )
            if metrics["blind_count"]:
                findings.append(
                    f"That said, {metrics['blind_count']} analysts had no data. An absence of "
                    f"bad news from a desk that could not look is not good news."
                )
            score = -0.5 if metrics["blind_count"] >= 3 else 0.0
            return self.verdict(score, 0.4, findings, [Evidence("opposing_analysts", 0)])

        findings.insert(
            0,
            f"{metrics['opposing']} of {metrics['total_analysts']} analysts found opposing evidence.",
        )
        for verdict in strongest:
            headline = verdict.key_findings[0] if verdict.key_findings else verdict.stance
            findings.append(f"{verdict.name} ({verdict.score:+.1f}): {headline}")

        for name, flag in flags[:5]:
            findings.append(f"Red flag from {name}: {flag}")

        if metrics["blind_count"] >= 3:
            findings.append(
                f"{metrics['blind_count']} analysts were blind. Those desks cannot vouch for "
                f"anything, and the bull case should not be credited for their silence."
            )

        score = max(-5.0, metrics["conviction_sum"] / max(metrics["opposing"], 1) * 1.3 - len(flags) * 0.4)
        if metrics["vetoes"]:
            score = -5.0
        confidence = min(0.85, 0.35 + len(flags) * 0.1 + metrics["opposing"] * 0.05)

        return self.verdict(score, confidence, findings,
                            [Evidence("opposing_analysts", f"{metrics['opposing']} of {metrics['total_analysts']}"),
                             Evidence("red_flags", len(flags))],
                            [f"{n}: {f}" for n, f in flags[:6]])


class ResearchValidationAnalyst(Analyst):
    """Audits the committee's own work: what was checked, and what was not.

    This bot does not judge the stock. It judges the evidence base, and its
    output is what stops the CMIO from mistaking a confident-looking report
    built on three working data feeds for a thorough one.
    """

    bot_id = "research_validation_analyst"
    desk = DESK
    name = "Research Validation Analyst"

    def gather(self, ctx: Any) -> dict[str, Any]:
        verdicts = _peer_verdicts(ctx)
        coverage = ctx.coverage()

        blind = [v for v in verdicts if not v.data_available]
        unsupported = [v for v in verdicts if v.data_available and v.key_findings and not v.evidence]
        low_confidence = [v for v in verdicts if v.data_available and v.confidence < 0.3]

        stale: list[str] = []
        for name in ("shareholding", "insider", "delivery", "fundamentals"):
            result = getattr(ctx, name, None)
            if result is not None and getattr(result, "as_of", None):
                age = result.age_days()
                if age is not None and age > 120:
                    stale.append(f"{name} is {age} days old")

        return {
            "verdict_count": len(verdicts),
            "blind": [v.name for v in blind],
            "blind_count": len(blind),
            "unsupported": [v.name for v in unsupported],
            "low_confidence": [v.name for v in low_confidence],
            "sources_available": sum(1 for ok in coverage.values() if ok),
            "sources_total": len(coverage),
            "missing_sources": [k for k, ok in coverage.items() if not ok],
            "stale": stale,
        }

    def judge(self, metrics: dict[str, Any]) -> Verdict:
        available = metrics["sources_available"]
        total = metrics["sources_total"]
        coverage = available / max(total, 1)

        findings = [
            f"{available} of {total} data sources returned usable data "
            f"({coverage:.0%} coverage).",
            f"{metrics['verdict_count'] - metrics['blind_count']} of {metrics['verdict_count']} "
            f"analysts had something to work with.",
        ]

        red_flags: list[str] = []

        if metrics["missing_sources"]:
            findings.append("Missing: " + ", ".join(metrics["missing_sources"]) + ".")
        if metrics["blind"]:
            findings.append("Analysts with no data: " + ", ".join(metrics["blind"]) + ".")
        if metrics["stale"]:
            findings.append("Stale inputs: " + "; ".join(metrics["stale"]) + ".")
        if metrics["unsupported"]:
            findings.append(
                "Claims made without a cited data field: " + ", ".join(metrics["unsupported"]) + "."
            )
            red_flags.append("Some findings are not traceable to a data field.")

        if coverage < 0.5:
            red_flags.append(
                f"Only {coverage:.0%} of sources are available. This report is too thin to act on."
            )
            findings.append(
                "At this coverage the committee's conviction should be treated as a guess "
                "with a decimal point, not a measurement."
            )
        elif coverage < 0.75:
            findings.append(
                "Coverage is partial. The conviction figure is real but its error bars are wider "
                "than they look."
            )

        # This bot scores the process, not the company. It goes negative only
        # when the evidence base is genuinely too weak to support a decision.
        score = (coverage - 0.75) * 6.0
        if metrics["blind_count"] >= 5:
            score -= 1.5

        return self.verdict(max(-4.0, min(2.0, score)), 0.7, findings,
                            [Evidence("source_coverage", f"{available}/{total}"),
                             Evidence("blind_analysts", metrics["blind_count"]),
                             Evidence("stale_inputs", len(metrics["stale"]))],
                            red_flags)


ANALYSTS = (BullCaseAnalyst, BearCaseAnalyst, ResearchValidationAnalyst)
