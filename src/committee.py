"""Running the committee and storing what it decided.

Three waves: the fourteen primary analysts, then the review desk (which reads
their verdicts), then the leads and the CMIO. No concurrency - every bot is
arithmetic over an evidence pack that was already fetched, so the whole run
takes a second or two and threads would only add failure modes.

Every verdict is persisted, not just the conclusion. That snapshot is the
learning loop's training data: a year from now the only way to ask which bot
actually saw a loss coming is to have kept what each one said before the
outcome was known.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime
from typing import Any

import pandas as pd
from sqlalchemy import desc, select

from src import db
from src.agents import cmio as cmio_module
from src.agents import evidence as evidence_module
from src.agents import leads as leads_module
from src.agents import registry
from src.agents.schemas import CommitteeReport, DeskReport, Verdict
from src.config import load_config
from src.data.provider import StockIdentity
from src.learning import weights as learned_weights
from src.strategy import regime as regime_rules
from src.strategy import sizing as sizing_rules

log = logging.getLogger(__name__)


def run(
    symbol: str | StockIdentity,
    *,
    cfg: Any = None,
    ctx: Any = None,
    portfolio: sizing_rules.PortfolioState | None = None,
    closed_trades: list[dict[str, Any]] | None = None,
    persist: bool = True,
    progress: Any = None,
    market_regime: Any = None,
) -> CommitteeReport:
    """Run all 23 bots over one stock.

    Pass `market_regime` when running many stocks, so the index is read once
    per scan rather than once per stock.
    """
    cfg = cfg or load_config()
    if market_regime is None:
        market_regime = regime_rules.current(cfg)
    started = time.time()
    identity = StockIdentity(symbol.upper()) if isinstance(symbol, str) else symbol

    if ctx is None:
        if progress:
            progress("Gathering evidence", 0.0)
        ctx = evidence_module.build(identity, cfg=cfg)

    desks_bots = registry.build(cfg)
    total = sum(len(b) for b in desks_bots.values())
    done = 0

    desk_verdicts: dict[str, list[Verdict]] = {}

    # --- Wave 1: the primary desks
    for desk in ("news", "equity", "macro", "ownership"):
        verdicts: list[Verdict] = []
        for bot in desks_bots[desk]:
            if progress:
                progress(bot.name, done / total)
            verdict = bot.run(ctx)
            verdicts.append(verdict)
            ctx.peer_verdicts[verdict.bot_id] = verdict
            done += 1
        desk_verdicts[desk] = verdicts

    # --- Wave 2: the review desk, which reads the above
    review: list[Verdict] = []
    for bot in desks_bots["review"]:
        if progress:
            progress(bot.name, done / total)
        verdict = bot.run(ctx)
        review.append(verdict)
        ctx.peer_verdicts[verdict.bot_id] = verdict
        done += 1
    desk_verdicts["review"] = review

    # --- Wave 3: leads, then the CMIO
    desk_reports: list[DeskReport] = [
        leads_module.synthesise(desk, desk_verdicts.get(desk, []))
        for desk in registry.DESK_ORDER
    ]

    latest = ctx.latest
    atr = 0.0
    if latest is not None and "atr" in latest and pd.notna(latest["atr"]):
        atr = float(latest["atr"])

    report = cmio_module.decide(
        identity.symbol,
        desk_reports,
        price=ctx.price,
        atr=atr,
        sector=ctx.sector,
        cfg=cfg,
        portfolio=portfolio,
        closed_trades=closed_trades or _closed_trades(),
        trade_date=ctx.as_of,
        market_regime=market_regime,
        desk_weights=learned_weights.active_weights(cfg),
    )
    report.duration_seconds = time.time() - started

    if persist:
        try:
            persist_report(report)
        except Exception as exc:
            log.exception("Could not persist the committee run for %s", identity.symbol)
            report.summary += f" (not saved: {exc})"

    return report


def _closed_trades() -> list[dict[str, Any]]:
    """The realised record the sizer estimates its edge from."""
    try:
        with db.connection() as conn:
            rows = conn.execute(
                select(db.trades.c.conviction_band, db.trades.c.return_pct)
            ).fetchall()
        return [
            {"conviction_band": r.conviction_band, "return_pct": r.return_pct}
            for r in rows
            if r.return_pct is not None
        ]
    except Exception:
        return []


# --- Persistence ------------------------------------------------------------


def persist_report(report: CommitteeReport) -> int:
    """Store the verdict and every bot's contribution to it."""
    db.init_db()

    with db.connection() as conn:
        run_id = conn.execute(
            db.committee_runs.insert().values(
                symbol=report.symbol,
                run_at=report.run_at,
                trade_date=report.trade_date,
                price_at_run=report.price,
                stance=report.stance,
                conviction=report.conviction,
                recommendation=(report.sizing or {}).get("recommendation"),
                forensics_veto=report.forensics_veto,
                bull_bear_agree=report.bull_bear_agree,
                sizing_json=db.to_json(report.sizing),
                exit_doctrine_json=db.to_json(report.exit_doctrine),
                summary=report.summary,
                dissent=report.dissent,
                weights_version=(db.active_weights() or {}).get("_version"),
                cost_usd=0.0,          # rule-based: the committee is free to run
                duration_seconds=report.duration_seconds,
            )
        ).inserted_primary_key[0]

        rows = []
        for desk in report.desks:
            for verdict in desk.verdicts:
                rows.append(_verdict_row(run_id, verdict))
            rows.append(_verdict_row(run_id, leads_module.as_verdict(desk)))
        rows.append(_verdict_row(run_id, cmio_module.as_verdict(report)))

        # One statement, not one per verdict. pg8000 has no fast
        # executemany, so a list of dicts costs a round trip each.
        conn.execute(db.bot_verdicts.insert().values(rows))

    return run_id


def _verdict_row(run_id: int, verdict: Verdict) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "bot_id": verdict.bot_id,
        "desk": verdict.desk,
        "role": verdict.role,
        "score": verdict.score,
        "confidence": verdict.confidence,
        "stance": verdict.stance,
        "data_available": verdict.data_available,
        "key_findings_json": db.to_json(verdict.key_findings),
        "evidence_json": db.to_json([e.__dict__ for e in verdict.evidence]),
        "red_flags_json": db.to_json(verdict.red_flags),
        "raw_json": db.to_json({"weight": verdict.weight, "note": verdict.data_note,
                                "veto": verdict.veto}),
    }


# --- Reading stored runs ----------------------------------------------------


def latest_run(symbol: str) -> dict[str, Any] | None:
    db.init_db()
    with db.connection() as conn:
        row = conn.execute(
            select(db.committee_runs)
            .where(db.committee_runs.c.symbol == symbol.upper())
            # By id, not run_at: ids increase monotonically whichever
            # machine wrote the row, clocks and time zones notwithstanding.
            .order_by(desc(db.committee_runs.c.id))
            .limit(1)
        ).first()

    if row is None:
        return None

    record = dict(row._mapping)
    record["sizing"] = db.from_json(record.pop("sizing_json", None), {})
    record["exit_doctrine"] = db.from_json(record.pop("exit_doctrine_json", None), {})
    return record


def verdicts_for(run_id: int) -> list[dict[str, Any]]:
    with db.connection() as conn:
        rows = conn.execute(
            select(db.bot_verdicts).where(db.bot_verdicts.c.run_id == run_id)
        ).fetchall()

    out = []
    for row in rows:
        record = dict(row._mapping)
        record["key_findings"] = db.from_json(record.pop("key_findings_json", None), [])
        record["evidence"] = db.from_json(record.pop("evidence_json", None), [])
        record["red_flags"] = db.from_json(record.pop("red_flags_json", None), [])
        record["raw"] = db.from_json(record.pop("raw_json", None), {})
        out.append(record)
    return out


def rebuild_report(run: dict[str, Any]) -> CommitteeReport | None:
    """Reconstruct a CommitteeReport from a stored run.

    Every verdict was persisted, so a past run can be reassembled rather than
    re-computed. That matters for the alert job - re-running the committee to
    write a message would produce a *different* verdict from the one being
    alerted about, since prices move between the scan and the send.
    """
    if not run:
        return None

    rows = verdicts_for(run["id"])
    if not rows:
        return None

    by_desk: dict[str, list[Verdict]] = {}
    for row in rows:
        if row.get("role") != "analyst":
            continue
        raw = row.get("raw") or {}
        by_desk.setdefault(row["desk"], []).append(
            Verdict(
                bot_id=row["bot_id"], desk=row["desk"],
                name=row["bot_id"].replace("_", " ").title(),
                role="analyst",
                score=row.get("score") or 0.0,
                confidence=row.get("confidence") or 0.0,
                stance=row.get("stance") or "NEUTRAL",
                key_findings=row.get("key_findings") or [],
                red_flags=row.get("red_flags") or [],
                data_available=bool(row.get("data_available")),
                data_note=raw.get("note"),
                veto=bool(raw.get("veto")),
            )
        )

    desks = [
        leads_module.synthesise(desk, by_desk.get(desk, []))
        for desk in registry.DESK_ORDER
        if desk in by_desk
    ]

    report = CommitteeReport(
        symbol=run["symbol"],
        run_at=run.get("run_at") or db.now(),
        trade_date=run.get("trade_date"),
        price=float(run.get("price_at_run") or 0.0),
        desks=desks,
        conviction=float(run.get("conviction") or 0.0),
        stance=run.get("stance") or "WATCH",
        forensics_veto=bool(run.get("forensics_veto")),
        bull_bear_agree=bool(run.get("bull_bear_agree")),
        summary=run.get("summary") or "",
        dissent=run.get("dissent") or "",
        sizing=run.get("sizing") or {},
        market_regime=(run.get("sizing") or {}).get("market_regime"),
        exit_doctrine=run.get("exit_doctrine") or {},
        duration_seconds=float(run.get("duration_seconds") or 0.0),
    )
    report.red_flags = [flag for v in report.all_verdicts for flag in v.red_flags]
    report.coverage = (
        sum(d.coverage for d in desks) / len(desks) if desks else 0.0
    )
    report.confidence = (
        sum(d.confidence for d in desks) / len(desks) if desks else 0.0
    )
    return report


def recent_runs(limit: int = 25) -> list[dict[str, Any]]:
    db.init_db()
    with db.connection() as conn:
        rows = conn.execute(
            select(db.committee_runs).order_by(desc(db.committee_runs.c.id)).limit(limit)
        ).fetchall()
    return [dict(r._mapping) for r in rows]
