"""Backtest the live BUY rule (option 1) with delivery confirmation, 2020 on.

Versions, fixed before running, all on today's Nifty 500 with the live exit
plan (-25% stop, a quarter sold at +25% and +50%, 20% trail after +30%),
Groww costs, entry at the next open, checked weekly, Rs 5 lakh capital:

  A  dip rules alone, 8% positions, at most 15   (the September research, on 2020+)
  B  dip rules + delivery confirmation, same sizing
  C  B with the sizing option 1 actually gives at conviction 50-59:
     3% positions, at most 8 (6% total heat / 0.75% risk each)

plus how many buy calls a year the rule makes, taking every signal (one open
trade per stock at a time), regardless of portfolio room. See
src/strategy/delivery_research.py for what cannot be replayed. Writes
reports/option1_research.md, .json and option1_trades.csv. No database.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from src.config import load_config
from src.data import nse, prices
from src.strategy import delivery_research as dr
from src.strategy import research
from src.strategy.exit_policies import LongTermPolicy, long_term_plans
from src.strategy.simulator import NEXT_OPEN, curve_stats, simulate, trade_stats

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("option1_research")

REPORTS = Path("reports")
START = date(2020, 1, 1)
PERIODS = (("2020-21", date(2020, 1, 1), date(2021, 12, 31)), ("2022+", date(2022, 1, 1), None),
           ("2020+", date(2020, 1, 1), None))


def num(x, digits=1) -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "-"
    return f"{x:.{digits}f}"


def pct(x) -> str:
    return "-" if x is None else f"{x:.0%}"


def fetch_bhavcopies(start: date, end: date):
    """Yield each session's bhavcopy, so ~1,700 full files are never held at once."""
    sessions = failed = n = 0
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            result = nse.get_bhavcopy(cursor)
            if result.value is not None and not result.value.empty:
                sessions += 1
                yield result.value
            elif result.status.name == "UNAVAILABLE":
                failed += 1
            n += 1
            if n % 100 == 0:
                log.info("Bhavcopies: %d weekdays checked to %s, %d sessions, %d failures",
                         n, cursor, sessions, failed)
        cursor += timedelta(days=1)
    log.info("Bhavcopies: %d sessions from %s to %s (%d failures)", sessions, start, end, failed)


def run(plan, bars, signals, ranks, start, end, cfg, regime_at, capital, position_pct, max_positions):
    return simulate(bars, signals, ranks, lambda symbol: LongTermPolicy(plan), strategy=f"option1_{plan.key}",
                    start=start, end=end, mode=NEXT_OPEN, position_value=capital * position_pct / 100.0,
                    max_positions=max_positions, initial_capital=capital, cfg=cfg, regime_at=regime_at)


def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest option 1 with delivery confirmation")
    parser.add_argument("--years", type=float, default=9.0, help="price history to fetch")
    args = parser.parse_args()

    started = time.time()
    cfg = load_config()
    today = date.today()
    end = nse.last_completed_session()
    REPORTS.mkdir(exist_ok=True)
    capital = float(cfg.get("sizing.default_capital", 500000.0))
    plan = long_term_plans(cfg)["L2"]

    universe = nse.get_universe("NIFTY 500").value or []
    if len(universe) < 400:
        log.error("Could not load the Nifty 500")
        return 1
    fetched = prices.get_price_history_batch(universe, years=args.years)
    stock_prices = {s: r.value for s, r in fetched.items() if r is not None and r.usable and r.value is not None}
    nifty = prices.get_index_history("nifty50", years=args.years).value
    nifty500 = prices.get_index_history("nifty500", years=args.years).value
    regime_at = research.regime_lookup(nifty500)
    log.info("Prices: %d of %d stocks", len(stock_prices), len(universe))

    bars, signals, ranks = research.long_term_arrays(stock_prices, cfg)
    deliveries = dr.delivery_frames(fetch_bhavcopies(START, end), set(bars))
    confirmed = dr.confirm(signals, bars, deliveries, cfg)
    log.info("Stocks with delivery history: %d. Dip signals from 2020: %d, delivery-confirmed: %d",
             len(deliveries),
             sum(int(s[[d >= START for d in bars[k].dates]].sum()) for k, s in signals.items()),
             sum(int(s.sum()) for s in confirmed.values()))

    heat = float(cfg.get("sizing.constraints.max_portfolio_heat_pct", 6.0))
    risk = float(cfg.get("sizing.bands.conservative.risk_per_position_pct", 0.75))
    stop = float(cfg.get("sizing.stop_pct", 25.0))
    live_pct = risk / stop * 100.0                       # 0.75% risk at a 25% stop = 3% of capital
    live_max = int(heat // risk)                         # 6% / 0.75% = 8 positions
    versions = {
        "A": ("Dip rules alone, 8% x 15", signals, 8.0, 15),
        "B": ("Dip rules + delivery, 8% x 15", confirmed, 8.0, 15),
        "C": (f"Option 1 as sized live: dip + delivery, {live_pct:.0f}% x {live_max}", confirmed, live_pct, live_max),
    }

    results, rows = {}, []
    for key, (label, sig, position_pct, max_positions) in versions.items():
        entry = {"label": label, "periods": {}}
        for name, start, stop_date in PERIODS:
            stop_date = stop_date or end
            result = run(plan, bars, sig, ranks, start, stop_date, cfg, regime_at, capital, position_pct, max_positions)
            curve = curve_stats(result.equity, capital)
            curve["exposure"] = result.exposure
            stats = trade_stats(result.trades)
            bench = curve_stats(research.benchmark_curve(nifty, start, stop_date, capital), capital)
            entry["periods"][name] = {"curve": curve, "trades": research._brief(stats), "benchmark": bench,
                                      "by_regime": stats.get("by_regime", {})}
            if name == "2020+":
                rows += [dict(t.to_row(), version=key) for t in result.trades]
        results[key] = entry
        p = entry["periods"]["2022+"]
        log.info("%s 2022+: %s%% / %s%% vs Nifty 50 %s%% / %s%%", key, num(p["curve"]["cagr_pct"]),
                 num(p["curve"]["max_drawdown_pct"]), num(p["benchmark"]["cagr_pct"]),
                 num(p["benchmark"]["max_drawdown_pct"]))

    # How many buy calls a year: every confirmed signal, one open trade per stock at a time.
    calls = simulate(bars, confirmed, ranks, lambda symbol: LongTermPolicy(plan), strategy="option1_calls",
                     start=START, end=end, mode=NEXT_OPEN, position_value=capital * live_pct / 100.0,
                     max_positions=None, cfg=cfg, regime_at=regime_at).trades
    call_rows = [t.to_row() for t in calls]
    per_year = dr.entries_per_year(call_rows)
    call_stats = research._brief(trade_stats(calls))
    dip_only_calls = simulate(bars, signals, ranks, lambda symbol: LongTermPolicy(plan), strategy="dip_calls",
                              start=START, end=end, mode=NEXT_OPEN, position_value=capital * live_pct / 100.0,
                              max_positions=None, cfg=cfg, regime_at=regime_at).trades
    dip_per_year = dr.entries_per_year([t.to_row() for t in dip_only_calls])

    pd.DataFrame(rows).to_csv(REPORTS / "option1_trades.csv", index=False)
    write_json(results, per_year, dip_per_year, call_stats, today)
    write_report(results, per_year, dip_per_year, call_stats, today, end, len(deliveries),
                 live_pct, live_max, time.time() - started)

    print("\n=== SUMMARY ===")
    for key, entry in results.items():
        line = "  ".join(f"{n} {num(p['curve']['cagr_pct'])}%/{num(p['curve']['max_drawdown_pct'])}%"
                         for n, p in entry["periods"].items())
        print(f"{key} {entry['label']:<48} {line}")
    bench = results["A"]["periods"]
    print("Nifty 50       " + "  ".join(f"{n} {num(p['benchmark']['cagr_pct'])}%/{num(p['benchmark']['max_drawdown_pct'])}%"
                                        for n, p in bench.items()))
    print(f"Buy calls per year (every confirmed signal): {per_year}")
    return 0


def write_json(results, per_year, dip_per_year, call_stats, today):
    def clean(obj):
        if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
            return None
        if isinstance(obj, dict):
            return {str(k): clean(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [clean(v) for v in obj]
        return obj

    payload = {"generated": today.isoformat(), "versions": results, "calls_per_year": per_year,
               "dip_only_calls_per_year": dip_per_year, "every_call": call_stats}
    (REPORTS / "option1_research.json").write_text(json.dumps(clean(payload), indent=2), encoding="utf-8")


def write_report(results, per_year, dip_per_year, call_stats, today, end, n_delivery, live_pct, live_max, seconds):
    lines = [
        "# Option 1, backtested: the live BUY rule with delivery confirmation",
        "",
        f"Generated {today.isoformat()} in {seconds / 60:.0f} minutes. Today's Nifty 500, prices from Yahoo, "
        f"delivery from NSE's daily bhavcopies ({n_delivery} stocks, January 2020 to {end.isoformat()}). Live "
        "exit plan, Groww costs, entry at the next open, checked weekly, Rs 5 lakh capital. Benchmark: the "
        "Nifty 50 index (price only, so about 1.3% a year of dividends are not counted for it).",
        "",
        "**What this cannot replay:** the quality gate (Yahoo keeps only about four years of statements) and "
        "the committee itself (past news and filings cannot be rebuilt). They are assumed to pass every "
        "candidate. Under option 1 the committee blocks only on a forensics veto or net-negative desks, so "
        "this is close to the live rule, but real results would differ by whatever those stages remove. "
        "Today's index members only, so survivorship flatters every figure, and tax is not deducted.",
        "",
        "| Version | Period | Growth a year | Worst fall | Nifty 50 growth | Nifty 50 worst fall | Trades | Win rate | Invested |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for key, entry in results.items():
        for name, p in entry["periods"].items():
            c, b, t = p["curve"], p["benchmark"], p["trades"]
            lines.append(f"| {key}: {entry['label']} | {name} | {num(c['cagr_pct'])}% | {num(c['max_drawdown_pct'])}% | "
                         f"{num(b['cagr_pct'])}% | {num(b['max_drawdown_pct'])}% | {t.get('trades') or 0} | "
                         f"{pct(t.get('win_rate'))} | {pct(c.get('exposure'))} |")
    lines += [
        "",
        f"Version C invests at most {live_max} x {live_pct:.0f}% = {live_max * live_pct:.0f}% of capital, because at "
        "conviction 50-59 each position risks 0.75% of capital to the -25% stop and total open risk is capped "
        "at 6%. The rest sits in cash, which this backtest earns nothing on.",
        "",
        "## How many buy calls a year",
        "",
        "Every delivery-confirmed dip signal taken as its own trade (one open trade per stock at a time), so "
        "this is how many distinct buy calls the rule makes, whatever room your portfolio has:",
        "",
        "| Year | Buy calls (dip + delivery) | Dip signals without delivery |",
        "|---|---|---|",
    ]
    for year in sorted(set(per_year) | set(dip_per_year)):
        lines.append(f"| {year} | {per_year.get(year, 0)} | {dip_per_year.get(year, 0)} |")
    if call_stats.get("trades"):
        lines += ["", f"Across all {call_stats['trades']} calls: {pct(call_stats.get('win_rate'))} made money after "
                      f"costs, average {num(call_stats.get('expectancy_pct'), 2)}% a trade, average win "
                      f"{num(call_stats.get('avg_win_pct'))}%, average loss {num(call_stats.get('avg_loss_pct'))}%."]
    (REPORTS / "option1_research.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
