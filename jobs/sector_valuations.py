"""Weekly: recompute each sector's median valuation across the Nifty 500.

Fetches fundamentals for every constituent (cached for a week, so a re-run
is quick), then stores the median P/E, price-to-book and free-cash-flow
yield per sector for the Valuation Analyst to compare against.

Runs from .github/workflows/sector-valuations.yml.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db
from src.config import load_config
from src.data import fundamentals, nse, valuations

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("sector_valuations")


def main() -> int:
    started = time.time()
    cfg = load_config()
    db.init_db()
    db.assert_encrypted()

    universe = nse.get_universe(cfg.get("universe.index", "NIFTY 500")).value or []
    if len(universe) < 100:
        log.error("Universe unavailable (%d names); leaving last week's medians in place", len(universe))
        return 1

    companies, failed = [], 0
    for i, stock in enumerate(universe, start=1):
        result = fundamentals.get_fundamentals(stock)
        if result.value:
            companies.append(result.value)
        else:
            failed += 1
        if i % 50 == 0:
            log.info("%d of %d fetched (%d failed)", i, len(universe), failed)

    rows = valuations.compute(companies)
    if len(companies) < len(universe) * 0.6:
        log.error("Only %d of %d companies had fundamentals; not replacing the medians",
                  len(companies), len(universe))
        return 1

    stored = valuations.store(rows)
    sectors = sorted({r["sector"] for r in rows})
    log.info("Stored %d medians for %d sectors from %d companies in %.0fs",
             stored, len(sectors), len(companies), time.time() - started)
    for r in rows:
        if r["metric"] == "pe":
            log.info("  %-28s P/E median %6.1f  (n=%d)", r["sector"], r["median"], r["n"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
