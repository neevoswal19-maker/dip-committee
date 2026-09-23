"""Running a scan and persisting it.

The dashboard never screens on page load. A scan takes a minute or two and
Streamlit re-runs the whole script on every widget interaction, so screening
inline would re-scan the universe every time a checkbox moved. Instead a scan
is an explicit action that writes to the database, and every page reads the
most recent stored result.

That split is also what lets the scheduled job and the dashboard share one
code path: `run_scan` is called by both.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable

from sqlalchemy import desc, func, select

from src import db, screener
from src.config import load_config
from src.data import nse

log = logging.getLogger(__name__)

ProgressFn = Callable[[int, int, str], None]


@dataclass
class ScanSummary:
    scan_id: int
    trade_date: date | None
    universe_size: int
    passed_dip: int
    passed_quality: int
    passed_delivery: int
    candidates: int
    duration_seconds: float
    errored: int = 0


def run_scan(
    *,
    index: str | None = None,
    limit: int | None = None,
    check_quality: bool = True,
    run_committee: bool = True,
    committee_top_n: int | None = None,
    progress: ProgressFn | None = None,
    cfg: Any = None,
) -> ScanSummary:
    """Screen the universe and store the result.

    The scan row is written before the work begins so that a crash leaves a
    visible `failed` record rather than silence. A scan that died is useful
    information; a scan that never appears looks like one that was never run.
    """
    cfg = cfg or load_config()
    index = index or cfg.get("universe.index", "NIFTY 500")
    started = time.time()

    db.init_db()

    with db.connection() as conn:
        scan_id = conn.execute(
            db.scans.insert().values(
                run_at=datetime.now(),
                trade_date=nse.last_trading_day(),
                universe=index,
                status="running",
            )
        ).inserted_primary_key[0]

    try:
        results = screener.screen_universe(
            index, cfg, limit=limit, check_quality=check_quality, progress=progress
        )
        summary = screener.summarise(results)

        rows = []
        for result in sorted(
            [r for r in results if r.passed_all], key=lambda r: r.score, reverse=True
        ):
            metrics = result.metrics
            rows.append(
                {
                    "scan_id": scan_id,
                    "symbol": result.symbol,
                    "rank": result.rank,
                    "screen_score": result.score,
                    "close": metrics.get("close"),
                    "rsi": metrics.get("rsi"),
                    "drawdown_pct": metrics.get("drawdown_pct"),
                    "dma_200": metrics.get("dma_200"),
                    "dma_slope_pct": metrics.get("dma_slope_pct"),
                    "atr": metrics.get("atr"),
                    "delivery_ratio": metrics.get("down_day_delivery_ratio_avg10"),
                    "delivery_persistence": metrics.get("delivery_persistence"),
                    "metrics_json": db.to_json(metrics),
                    "stage_failures_json": None,
                }
            )

        # Keep near-misses too: a scan that found nothing is far more
        # informative when you can see what came closest and why it failed.
        near_misses = [
            r for r in results
            if not r.passed_all and r.dip and r.dip.passed and not r.error
        ][:25]
        for result in near_misses:
            rows.append(
                {
                    "scan_id": scan_id,
                    "symbol": result.symbol,
                    "rank": None,
                    "screen_score": 0.0,
                    "close": result.metrics.get("close"),
                    "rsi": result.metrics.get("rsi"),
                    "drawdown_pct": result.metrics.get("drawdown_pct"),
                    "dma_200": result.metrics.get("dma_200"),
                    "dma_slope_pct": result.metrics.get("dma_slope_pct"),
                    "atr": result.metrics.get("atr"),
                    "delivery_ratio": result.metrics.get("down_day_delivery_ratio_avg10"),
                    "delivery_persistence": result.metrics.get("delivery_persistence"),
                    "metrics_json": db.to_json(result.metrics),
                    "stage_failures_json": db.to_json(
                        {
                            "summary": result.failure_summary,
                            "quality": result.quality.reasons if result.quality else [],
                            "delivery": result.delivery.reasons if result.delivery else [],
                        }
                    ),
                }
            )

        if rows:
            with db.connection() as conn:
                # One statement, not one per candidate - see
                # nse.store_delivery_bars for why this matters.
                conn.execute(db.candidates.insert().values(rows))

        # Refresh the stock reference table from what the scan just saw.
        _upsert_stocks(results)

        # --- The committee, on whatever survived the screen.
        #
        # This runs as part of the scan rather than on demand because the
        # alternative caused a real problem: the dashboard showed the screen
        # score in the place a verdict belongs, and that score is the one
        # measured as NOT predicting outcomes. A candidate list without a
        # committee verdict attached invites being read as a recommendation.
        survivors = sorted(
            [r for r in results if r.passed_all], key=lambda r: r.score, reverse=True
        )
        top_n = committee_top_n if committee_top_n is not None else int(
            cfg.get("scan.committee_top_n", 5)
        )
        if run_committee and survivors:
            _run_committee_on(survivors[:top_n], cfg, progress)

        duration = time.time() - started
        with db.connection() as conn:
            conn.execute(
                db.scans.update()
                .where(db.scans.c.id == scan_id)
                .values(
                    universe_size=summary["universe_size"],
                    passed_quality=summary["passed_quality"],
                    passed_dip=summary["passed_dip"],
                    passed_delivery=summary["passed_delivery"],
                    status="complete",
                    duration_seconds=duration,
                )
            )

        return ScanSummary(
            scan_id=scan_id,
            trade_date=nse.last_trading_day(),
            universe_size=summary["universe_size"],
            passed_dip=summary["passed_dip"],
            passed_quality=summary["passed_quality"],
            passed_delivery=summary["passed_delivery"],
            candidates=summary["candidates"],
            duration_seconds=duration,
            errored=summary["errored"],
        )

    except Exception as exc:
        log.exception("Scan %s failed", scan_id)
        with db.connection() as conn:
            conn.execute(
                db.scans.update()
                .where(db.scans.c.id == scan_id)
                .values(status="failed", error=str(exc), duration_seconds=time.time() - started)
            )
        raise


def _run_committee_on(
    candidates: list[screener.ScreenResult],
    cfg: Any,
    progress: ProgressFn | None = None,
) -> None:
    """Run the committee over the shortlist and store each verdict.

    A failure on one stock must not lose the scan. The committee is
    supplementary to the screen, and a candidate without a verdict is
    displayed as exactly that rather than silently as an endorsed one.
    """
    from src import committee as committee_module
    from src.strategy import regime as regime_rules

    market = regime_rules.current(cfg)
    log.info(regime_rules.describe_for_humans(market))

    for i, candidate in enumerate(candidates, start=1):
        if progress is not None:
            progress(i, len(candidates), f"committee: {candidate.symbol}")
        try:
            committee_module.run(candidate.symbol, cfg=cfg, market_regime=market)
        except Exception as exc:
            log.warning("Committee failed for %s: %s", candidate.symbol, exc)


def convictions_for(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """Latest committee verdict per symbol, for the ones that have had a run.

    Symbols absent from the result have never been through the committee.
    Callers must show that as unknown rather than defaulting to neutral -
    "not assessed" and "assessed as middling" are different statements.
    """
    if not symbols:
        return {}

    db.init_db()
    out: dict[str, dict[str, Any]] = {}

    with db.connection() as conn:
        for symbol in symbols:
            row = conn.execute(
                select(
                    db.committee_runs.c.id,
                    db.committee_runs.c.conviction,
                    db.committee_runs.c.stance,
                    db.committee_runs.c.recommendation,
                    db.committee_runs.c.forensics_veto,
                    db.committee_runs.c.run_at,
                    db.committee_runs.c.summary,
                )
                .where(db.committee_runs.c.symbol == symbol.upper())
                .order_by(desc(db.committee_runs.c.run_at))
                .limit(1)
            ).first()
            if row is not None:
                out[symbol.upper()] = dict(row._mapping)

    return out


def _upsert_stocks(results: list[screener.ScreenResult]) -> None:
    with db.connection() as conn:
        known = {row.symbol for row in conn.execute(select(db.stocks.c.symbol))}

        new_rows = []
        for result in results:
            values = {
                "symbol": result.symbol,
                "name": result.name,
                "sector": result.sector,
                "in_universe": True,
                "updated_at": datetime.now(),
            }
            if result.symbol in known:
                conn.execute(
                    db.stocks.update().where(db.stocks.c.symbol == result.symbol).values(**values)
                )
            else:
                new_rows.append(values)

        if new_rows:
            conn.execute(db.stocks.insert(), new_rows)


# --- Reading stored scans ---------------------------------------------------


def latest_scan() -> dict[str, Any] | None:
    db.init_db()
    with db.connection() as conn:
        row = conn.execute(
            select(db.scans).where(db.scans.c.status == "complete").order_by(desc(db.scans.c.run_at)).limit(1)
        ).first()
    return dict(row._mapping) if row else None


def scan_history(limit: int = 30) -> list[dict[str, Any]]:
    db.init_db()
    with db.connection() as conn:
        rows = conn.execute(
            select(db.scans).order_by(desc(db.scans.c.run_at)).limit(limit)
        ).fetchall()
    return [dict(r._mapping) for r in rows]


def candidates_for(scan_id: int, *, include_near_misses: bool = False) -> list[dict[str, Any]]:
    db.init_db()
    query = select(db.candidates).where(db.candidates.c.scan_id == scan_id)
    if not include_near_misses:
        query = query.where(db.candidates.c.rank.isnot(None))
    query = query.order_by(db.candidates.c.rank.asc().nulls_last(), desc(db.candidates.c.screen_score))

    with db.connection() as conn:
        rows = conn.execute(query).fetchall()

    out = []
    for row in rows:
        record = dict(row._mapping)
        record["metrics"] = db.from_json(record.pop("metrics_json", None), {})
        record["failures"] = db.from_json(record.pop("stage_failures_json", None), {})
        out.append(record)
    return out


def latest_candidates(include_near_misses: bool = False) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    scan = latest_scan()
    if scan is None:
        return None, []
    return scan, candidates_for(scan["id"], include_near_misses=include_near_misses)
