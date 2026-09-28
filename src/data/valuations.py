"""Sector valuation medians - what "cheap" means next to a company's peers.

A P/E of 45 is expensive for a bank and ordinary for a consumer-goods
company. So a stock is compared with the median of its own sector across the
Nifty 500, not with one scale for everything.

The medians are computed weekly by jobs/sector_valuations.py and stored in
`sector_valuations`; the Valuation Analyst only reads them. Sectors are
Yahoo's, the same labels the fundamentals carry, so the two sides match.

Figures outside sensible bounds are dropped before the median - a P/E of
900 from a year of collapsed profit says nothing about what the sector pays.
"""

from __future__ import annotations

import logging
from statistics import median
from typing import Any, Iterable

from sqlalchemy import delete, select

from src import db

log = logging.getLogger(__name__)

#: Fewer companies than this and a sector's median is too thin to lean on.
MIN_PEERS = 5

#: Plausible ranges; values outside are distortions, not valuations.
BOUNDS = {
    "pe": (0.0, 200.0),
    "pb": (0.0, 50.0),
    "fcf_yield": (-50.0, 50.0),
}

FIELDS = {"pe": "pe_trailing", "pb": "price_to_book", "fcf_yield": "fcf_yield_pct"}


def _quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    low, high = int(position), min(int(position) + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def compute(companies: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Median, quartiles and count per sector and metric."""
    buckets: dict[tuple[str, str], list[float]] = {}
    for company in companies:
        sector = (company.get("sector") or "").strip()
        if not sector:
            continue
        for metric, field in FIELDS.items():
            value = company.get(field)
            if value is None:
                continue
            value = float(value)
            low, high = BOUNDS[metric]
            if low < value < high or (metric == "fcf_yield" and low <= value <= high):
                buckets.setdefault((sector, metric), []).append(value)

    rows = []
    for (sector, metric), values in sorted(buckets.items()):
        if len(values) < MIN_PEERS:
            continue
        rows.append({
            "sector": sector, "metric": metric, "n": len(values),
            "median": round(median(values), 4),
            "p25": round(_quantile(values, 0.25), 4),
            "p75": round(_quantile(values, 0.75), 4),
        })
    return rows


def store(rows: list[dict[str, Any]]) -> int:
    """Replace the table with a fresh set of medians."""
    if not rows:
        return 0
    stamp = db.now()
    with db.connection() as conn:
        conn.execute(delete(db.sector_valuations))
        conn.execute(db.sector_valuations.insert().values([dict(r, computed_at=stamp) for r in rows]))
    return len(rows)


def sector_medians(sector: str | None) -> dict[str, dict[str, Any]]:
    """{metric: {median, p25, p75, n, computed_at}} for one sector; empty if unknown."""
    if not sector:
        return {}
    try:
        with db.connection() as conn:
            rows = conn.execute(
                select(db.sector_valuations).where(db.sector_valuations.c.sector == sector)
            ).fetchall()
    except Exception as exc:     # no table yet, or no database
        log.debug("Sector medians unavailable: %s", exc)
        return {}
    return {r.metric: {"median": r.median, "p25": r.p25, "p75": r.p75, "n": r.n,
                       "computed_at": r.computed_at} for r in rows}
