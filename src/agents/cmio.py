"""The Chief Market Intelligence Officer.

Takes the five desk reports and produces the one thing the rest of the system
needs: a conviction number. Everything downstream - the Kelly fraction, the
rupee size, the tranches, the exit doctrine - follows from it, which is why
the mapping stays deliberately legible rather than clever.

The CMIO does not re-derive the money decisions. It hands conviction to
`sizing.size_position` and `exit.build_doctrine`, both of which are already
tested, so there is exactly one implementation of how much to buy.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from src.agents.schemas import (
    CommitteeReport,
    DeskReport,
    Verdict,
    score_to_conviction,
)
from src.strategy import exit as exit_rules
from src.strategy import regime as regime_rules
from src.strategy import sizing as sizing_rules


def decide(
    symbol: str,
    desks: list[DeskReport],
    *,
    price: float,
    atr: float,
    sector: str | None,
    cfg: Any,
    portfolio: sizing_rules.PortfolioState | None = None,
    closed_trades: list[dict[str, Any]] | None = None,
    trade_date: date | None = None,
    market_regime: Any = None,
    desk_weights: dict[str, float] | None = None,
) -> CommitteeReport:
    """Weigh the desks, set conviction, and size the position.

    `desk_weights` are the learner's current weights when given (see
    learning/weights.py); otherwise the config's.
    """
    report = CommitteeReport(
        symbol=symbol, price=price, sector=sector, trade_date=trade_date, desks=desks
    )

    weights = desk_weights or cfg.get("committee.desk_weights", {}) or {}
    contributing: list[tuple[DeskReport, float]] = []

    for desk in desks:
        if desk.coverage <= 0 or desk.confidence <= 0:
            report.blind_desks.append(desk.name)
            continue
        weight = float(weights.get(desk.desk, 0.0)) * desk.confidence
        if weight > 0:
            contributing.append((desk, weight))

    total_weight = sum(w for _, w in contributing)

    if total_weight <= 0:
        report.conviction = 0.0
        report.stance = "NO_BUY"
        report.confidence = 0.0
        report.coverage = 0.0
        report.summary = (
            "No desk produced a usable verdict. This is a data failure, not a view on the "
            "company - nothing should be bought or sold on the strength of it."
        )
        return report

    report.weighted_score = sum(desk.score * w for desk, w in contributing) / total_weight
    report.conviction = score_to_conviction(report.weighted_score)
    report.coverage = sum(d.coverage for d in desks) / len(desks) if desks else 0.0
    report.confidence = sum(d.confidence * w for d, w in contributing) / total_weight

    all_verdicts = report.all_verdicts
    report.red_flags = _dedupe_flags(all_verdicts)

    # --- The forensics veto
    vetoing = [v for v in all_verdicts if v.veto]
    if vetoing and cfg.get("committee.forensics_veto", True):
        report.forensics_veto = True
        report.conviction = 0.0
        report.stance = "NO_BUY"

    # --- Bull vs Bear
    bull = report.verdict_for("bull_case_analyst")
    bear = report.verdict_for("bear_case_analyst")
    if bull and bear:
        # They agree when the bull case is clearly stronger than the bear's
        # objection - not when their scores are similar, which would be the
        # two of them being equally unsure.
        report.bull_bear_agree = (bull.score + bear.score) > 1.5
    else:
        report.bull_bear_agree = False

    # --- Stance
    if not report.forensics_veto:
        alert_floor = float(cfg.get("committee.min_conviction_to_alert", 60))
        if report.conviction >= alert_floor:
            report.stance = "BUY"
        elif report.conviction >= 45:
            report.stance = "WATCH"
        else:
            report.stance = "NO_BUY"

    # --- Sizing, from the existing tested engine
    portfolio = portfolio or sizing_rules.PortfolioState(
        capital=float(cfg.get("sizing.default_capital", 500000.0))
    )
    decision = sizing_rules.size_position(
        symbol=symbol,
        conviction=report.conviction,
        price=price,
        atr=atr,
        cfg=cfg,
        portfolio=portfolio,
        sector=sector,
        has_red_flags=bool(report.red_flags),
        forensics_veto=report.forensics_veto,
        bull_bear_agree=report.bull_bear_agree,
        closed_trades=closed_trades,
    )
    # --- Market regime. Applied after sizing so the size shown is the one
    # the policy actually allows, and recorded either way so real trades can
    # later test whether the regime mattered.
    was_buy = decision.is_buy
    decision = regime_rules.apply_policy(decision, market_regime, cfg)
    if was_buy and not decision.is_buy and report.stance == "BUY":
        # Conviction did not change; the market did. WATCH says exactly that.
        report.stance = "WATCH"

    report.sizing = decision.to_dict()
    if market_regime is not None:
        report.market_regime = market_regime.to_dict()
        # Stored inside sizing_json so no schema migration is needed on Neon.
        report.sizing["market_regime"] = report.market_regime

    if decision.is_buy:
        report.exit_doctrine = exit_rules.build_doctrine(
            symbol, price, cfg, stop_price=decision.stop_price
        ).to_dict()

    report.summary = _summarise(report, decision, vetoing)
    if market_regime is not None and not report.forensics_veto:
        report.summary += " " + regime_rules.describe_for_humans(market_regime)
    report.dissent = _dissent(report)
    return report


def _dedupe_flags(verdicts: list[Verdict]) -> list[str]:
    """Collect red flags once each.

    The Bear Case Analyst re-emits other bots' flags with the source name
    prefixed, so the same concern arrives twice in different wording. The
    count matters - it feeds the sizing band's red-flag test - so a
    double-counted worry would shrink a position for no reason.
    """
    seen: dict[str, str] = {}

    for verdict in verdicts:
        for flag in verdict.red_flags:
            # "Analyst Name: the concern" and "the concern" are one flag.
            body = flag.split(": ", 1)[-1].strip() if ": " in flag else flag.strip()
            key = body.lower()
            # Keep whichever phrasing names its source.
            if key not in seen or len(flag) > len(seen[key]):
                seen[key] = flag

    return list(seen.values())


def _summarise(report: CommitteeReport, decision: Any, vetoing: list[Verdict]) -> str:
    if report.forensics_veto:
        names = ", ".join(v.name for v in vetoing)
        return (
            f"NO BUY on a veto from {names}. A critical accounting flag overrides every other "
            f"reading: the rest of the committee may be right about the business and it would "
            f"not matter, because the risk being guarded against here is permanent loss of "
            f"capital rather than underperformance."
        )

    parts = [
        f"Conviction {report.conviction:.0f} of 100 ({report.weighted_score:+.2f} weighted "
        f"across {len(report.desks) - len(report.blind_desks)} desks)."
    ]

    ranked = sorted(
        [d for d in report.desks if d.confidence > 0],
        key=lambda d: -abs(d.score) * d.confidence,
    )
    if ranked:
        strongest = ranked[0]
        parts.append(
            f"The {strongest.name} carried the most weight at {strongest.score:+.2f}."
        )
        if len(ranked) > 1:
            weakest = ranked[-1]
            if (weakest.score > 0) != (strongest.score > 0):
                parts.append(
                    f"The {weakest.name} pulled the other way at {weakest.score:+.2f}."
                )

    if report.blind_desks:
        parts.append(
            f"{len(report.blind_desks)} desk(s) had no usable data ({', '.join(report.blind_desks)}), "
            f"so this verdict rests on a narrower base than it appears to."
        )

    if report.coverage < 0.7:
        parts.append(
            f"Overall source coverage is {report.coverage:.0%}. Treat the conviction figure "
            f"as indicative rather than measured."
        )

    if decision.is_buy:
        parts.append(decision.narrative)
    else:
        parts.append("Sizing returns NO BUY: " + "; ".join(decision.rejections))

    return " ".join(parts)


def _dissent(report: CommitteeReport) -> str:
    """What the committee overruled, stated plainly.

    A verdict that hides the minority view is less useful than one that names
    it, because the minority is where the next mistake usually comes from.
    """
    lines: list[str] = []

    for desk in report.desks:
        for entry in desk.dissent:
            lines.append(f"[{desk.name}] {entry}")

    direction = 1 if report.weighted_score > 0 else -1
    contrarians = [
        d for d in report.desks
        if d.confidence > 0 and (1 if d.score > 0 else -1) != direction and abs(d.score) > 1.0
    ]
    for desk in contrarians:
        lines.append(f"[{desk.name}] disagreed with the overall direction at {desk.score:+.2f}.")

    if report.red_flags:
        lines.append(f"{len(report.red_flags)} red flag(s) were raised and weighed in: ")
        lines.extend(f"  - {flag}" for flag in report.red_flags[:5])

    if not lines:
        return "No material dissent - the desks broadly agreed."
    return "\n".join(lines)


def as_verdict(report: CommitteeReport) -> Verdict:
    """The CMIO's own row, for uniform persistence alongside the other 22."""
    return Verdict(
        bot_id="cmio",
        desk="cmio",
        name="Chief Market Intelligence Officer",
        role="cmio",
        score=report.weighted_score,
        confidence=report.confidence,
        stance="POSITIVE" if report.stance == "BUY" else ("NEGATIVE" if report.stance == "NO_BUY" else "NEUTRAL"),
        key_findings=[report.summary],
        red_flags=report.red_flags,
        data_available=report.coverage > 0,
        data_note=None if report.coverage >= 0.9 else f"coverage {report.coverage:.0%}",
        veto=report.forensics_veto,
    )
