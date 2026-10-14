"""Per-stock US test: every dip signal as its own trade, against SPY.

The portfolio test (jobs/us_research.py) holds at most 15 stocks, so it says
how one portfolio did, not whether dips in a given stock pay. This runs every
signal independently on the S&P 500 + Nasdaq-100 and the January 2014 Dow,
compares each trade with SPY over the same dates, and judges the 2014 Dow
group against the pre-registered rule in src/strategy/us_research.py. Writes:

  reports/us_per_stock.md     the report
  reports/us_per_stock.csv    every trade, with SPY over the same dates

Research only: no alerts, no database.
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from src import us_charges
from src.config import load_config
from src.data import prices, us_universe
from src.data.provider import StockIdentity
from src.strategy import research, us_research
from src.strategy.exit_policies import long_term_plans

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("us_per_stock")

REPORTS = Path("reports")


def num(x, digits=1, sign=True) -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "-"
    return f"{x:+.{digits}f}" if sign else f"{x:.{digits}f}"


def pct(x) -> str:
    return "-" if x is None else f"{x:.0%}"


def period_of(row) -> str:
    return "2014-21" if row["entry_date"] < research.TEST_START else "2022+"


ROW_HEADER = ["| Group | Trades | Win rate | Avg trade | SPY same dates | Dip minus SPY | Beat SPY | Avg hold |",
              "|---|---|---|---|---|---|---|---|"]


def row(label: str, s: dict) -> str:
    if not s.get("trades"):
        return f"| {label} | 0 | - | - | - | - | - | - |"
    return (f"| {label} | {s['trades']} | {pct(s['win_rate'])} | {num(s['avg_return_pct'])}% | "
            f"{num(s['avg_spy_pct'])}% | **{num(s['avg_excess_pct'])}%** | {pct(s['beat_spy_rate'])} | "
            f"{s['avg_sessions'] / 21:.0f} months |")


def main() -> int:
    parser = argparse.ArgumentParser(description="US per-stock dip test")
    parser.add_argument("--years", type=float, default=15.0)
    args = parser.parse_args()

    started = time.time()
    cfg = load_config()
    today = date.today()
    REPORTS.mkdir(exist_ok=True)

    universe = us_universe.stocks()
    known = {s.symbol for s in universe}
    dow = [str(s).upper() for s in cfg.get("us.dow_2014", [])]
    universe += [StockIdentity(symbol=s, market="US") for s in dow if s not in known]
    sectors = {s.symbol: s.sector or "Unknown" for s in universe}
    log.info("Universe: %d stocks (Dow 2014: %d)", len(universe), len(dow))
    if len(universe) < 400:
        log.error("Could not load the US universe")
        return 1

    fetched = prices.get_price_history_batch(universe, years=args.years)
    stock_prices = {s: r.value for s, r in fetched.items() if r is not None and r.usable and r.value is not None}
    spy = us_research.total_return(prices.get_index_history("SPY", years=args.years).value)
    regime_at = research.regime_lookup(spy)

    bars, signals, ranks = research.long_term_arrays(stock_prices, cfg)
    plan = long_term_plans(cfg)["L2"]
    trades = us_research.every_signal(plan, bars, signals, ranks, start=research.TRAIN[0], end=today,
                                      cfg=cfg, cost_fn=us_charges.cost_fn(cfg), regime_at=regime_at)
    rows = us_research.with_excess([t.to_row() for t in trades if t.entry_date], spy)
    for r in rows:
        r["period"] = period_of(r)
        r["sector"] = sectors.get(r["symbol"], "Unknown")
    log.info("%d trades across %d stocks", len(rows), len({r['symbol'] for r in rows}))

    dow_rows = [r for r in rows if r["symbol"] in set(dow)]
    dow_by_period = us_research.group_by(dow_rows, lambda r: r["period"])
    for name in ("2014-21", "2022+"):
        dow_by_period.setdefault(name, {"trades": 0})
    passed, reasons = us_research.judge_group({k: dow_by_period[k] for k in ("2014-21", "2022+")})
    log.info("Dow 2014: %s %s", "PASS" if passed else "FAIL", "; ".join(reasons))

    pd.DataFrame(rows).to_csv(REPORTS / "us_per_stock.csv", index=False)
    write_report(rows, dow, dow_rows, dow_by_period, passed, reasons, cfg, today,
                 len(stock_prices), time.time() - started)

    print("\n=== SUMMARY ===")
    for name in ("2014-21", "2022+"):
        s = dow_by_period[name]
        print(f"Dow 2014 {name}: {s.get('trades', 0)} trades, avg {num(s.get('avg_return_pct'))}% "
              f"vs SPY {num(s.get('avg_spy_pct'))}%, excess {num(s.get('avg_excess_pct'))}%")
    print(f"Dow 2014 group: {'PASS' if passed else 'FAIL'}")
    return 0


def write_report(rows, dow, dow_rows, dow_by_period, passed, reasons, cfg, today, n, seconds):
    by_period = us_research.group_by(rows, lambda r: r["period"])
    lines = [
        "# US stocks, one at a time: do dips in individual stocks pay?",
        "",
        f"Generated {today.isoformat()} in {seconds / 60:.0f} minutes from {n} price histories. Every dip "
        "signal from the live rules is taken as its own trade (one open trade per stock at a time), with "
        "the live exit plan (-25% stop, a quarter sold at +25% and +50%, 20% trail after +30%), $8,000 "
        "a trade, Groww's US costs and 0.5% currency conversion per order, entering at the next open. "
        "Each trade is compared with buying SPY (dividends reinvested) on the same entry day and selling "
        "it on the same exit day. **Dip minus SPY** is the number that matters: positive means buying "
        "the dip beat simply buying the index at that moment.",
        "",
        "## The pre-registered test: the 30 Dow companies of January 2014",
        "",
        "Chosen because nobody picked that list knowing what happened next. The rule, fixed before "
        "running: in both 2014-21 and 2022 onward, the average dip trade must beat SPY over the same "
        f"dates after costs, with at least {us_research.PER_STOCK_PASS['min_trades']} trades in each.",
        "",
        f"### Result: {'PASS' if passed else 'fail'}",
        "",
    ]
    if reasons:
        lines += [f"- {r}" for r in reasons] + [""]
    lines += ROW_HEADER + [row(k, dow_by_period[k]) for k in ("2014-21", "2022+")] + [""]

    per_dow = us_research.group_by(dow_rows, lambda r: r["symbol"])
    lines += ["Each Dow company, all years:", ""] + ROW_HEADER
    for symbol in sorted(per_dow, key=lambda s: -(per_dow[s].get("avg_excess_pct") or 0)):
        lines.append(row(symbol, per_dow[symbol]))
    missing = [s for s in dow if s not in per_dow]
    if missing:
        lines += ["", f"No dip signals: {', '.join(missing)}."]

    lines += ["", "## For information: all S&P 500 and Nasdaq-100 stocks", "",
              "Today's members, so survivorship flatters these: companies that shrank out of the index "
              "are missing.", ""]
    lines += ROW_HEADER + [row(k, by_period[k]) for k in sorted(by_period)] + [""]

    by_sector = us_research.group_by(rows, lambda r: r["sector"])
    lines += ["By sector, all years:", ""] + ROW_HEADER
    for sector in sorted(by_sector, key=lambda s: -(by_sector[s].get("avg_excess_pct") or 0)):
        lines.append(row(sector, by_sector[sector]))

    by_regime = us_research.group_by(rows, lambda r: r.get("regime") or "unknown")
    lines += ["", "By US market trend on the buy date, all years:", ""] + ROW_HEADER
    for regime in sorted(by_regime):
        lines.append(row(regime, by_regime[regime]))

    per_stock = us_research.group_by(rows, lambda r: r["symbol"])
    ranked = [s for s in per_stock if per_stock[s]["trades"] >= 3]
    ranked.sort(key=lambda s: per_stock[s]["avg_excess_pct"])
    lines += ["", f"Stocks with at least 3 dip trades: {len(ranked)}. Of these, "
              f"{sum(1 for s in ranked if per_stock[s]['avg_excess_pct'] > 0)} beat SPY on average.", "",
              "Best 15 (hindsight: do not read these as picks):", ""] + ROW_HEADER
    lines += [row(s, per_stock[s]) for s in reversed(ranked[-15:])]
    lines += ["", "Worst 15:", ""] + ROW_HEADER + [row(s, per_stock[s]) for s in ranked[:15]]

    mega = [str(s).upper() for s in cfg.get("us.megacaps_today", [])]
    lines += ["", "## For interest only: today's largest companies", "",
              "These are the biggest companies now *because* their shares rose, so dips in them paying off "
              "is partly true by construction. Never judged.", ""] + ROW_HEADER
    for symbol in mega:
        lines.append(row(symbol, per_stock.get(symbol, {"trades": 0})))
    (REPORTS / "us_per_stock.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
