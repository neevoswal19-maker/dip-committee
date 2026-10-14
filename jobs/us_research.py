"""Run the US research and write the report.

Tests the live dip strategy on the S&P 500 + Nasdaq-100 and on 30 ETFs,
after Groww's US charges, against holding SPY. See src/strategy/us_research.py
for the procedure and the pass criteria. Writes:

  reports/us_research.md      the report
  reports/us_research.json    machine-readable results
  reports/us_trades.csv       every trade behind the numbers

Runs on GitHub Actions (.github/workflows/us-research.yml). Needs no
secrets and writes nothing to the database.
"""

from __future__ import annotations

import argparse
import json
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
from src.strategy import research, us_research
from src.strategy.exit_policies import long_term_plans
from src.strategy.simulator import curve_stats

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("us_research")

REPORTS = Path("reports")


def fetch(universe: list, years: float) -> dict[str, pd.DataFrame]:
    results = prices.get_price_history_batch(universe, years=years)
    return {s: r.value for s, r in results.items() if r is not None and r.usable and r.value is not None}


def pct(x) -> str:
    return "-" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:.0%}"


def num(x, digits=1) -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "-"
    return f"{x:.{digits}f}"


def main() -> int:
    parser = argparse.ArgumentParser(description="US dip-strategy research")
    parser.add_argument("--years", type=float, default=15.0)
    parser.add_argument("--limit", type=int, default=0, help="cap the stock universe (0 = all)")
    args = parser.parse_args()

    started = time.time()
    cfg = load_config()
    today = date.today()
    REPORTS.mkdir(exist_ok=True)
    rc = cfg.get("us.research", {}) or {}
    capital = float(rc.get("capital_usd", 100_000))
    plans = long_term_plans(cfg)
    live = plans["L2"]                     # the live exit plan since the India research

    # --- Universe and prices
    stocks = us_universe.stocks()
    if args.limit:
        stocks = stocks[: args.limit]
    etfs = us_universe.etfs(cfg)
    log.info("Universe: %d stocks, %d ETFs", len(stocks), len(etfs))
    if len(stocks) < 400 and not args.limit:
        log.error("Could not load the S&P 500 / Nasdaq-100 lists")
        return 1

    stock_prices = fetch(stocks, args.years)
    etf_prices = fetch(etfs, args.years)
    spy = us_research.total_return(prices.get_index_history("SPY", years=args.years).value)
    usdinr = prices.get_index_history("USDINR=X", years=args.years).value
    regime_at = research.regime_lookup(spy)
    log.info("Usable histories: %d stocks, %d ETFs", len(stock_prices), len(etf_prices))

    bars, signals, ranks = research.long_term_arrays(stock_prices, cfg)
    confirmed = us_research.confirm_signals(signals, bars, stock_prices, cfg)
    log.info("Dip signals: %d, of which volume-confirmed: %d",
             sum(int(s.sum()) for s in signals.values()), sum(int(s.sum()) for s in confirmed.values()))

    costs = us_charges.cost_fn(cfg)
    common = dict(today=today, cfg=cfg, regime_at=regime_at, benchmark=spy, capital=capital)
    stock_kw = dict(common, position_pct=float(rc.get("position_pct", 8.0)),
                    max_positions=int(rc.get("max_positions", 15)), min_trades=us_research.PASS["min_trades_stocks"])

    # --- Pre-registered versions
    outcomes = [
        us_research.run_version("dip", "Dip rules alone", live, bars, signals, ranks, cost_fn=costs, **stock_kw),
        us_research.run_version("dip_volume", "Dip rules + down-day volume confirmation", live, bars,
                                confirmed, ranks, cost_fn=costs, **stock_kw),
    ]
    for o in outcomes:
        log.info("%s: %s %s", o.key, "PASS" if o.passed else "FAIL", "; ".join(o.reasons))

    # --- ETFs
    ebars, esignals, eranks = research.long_term_arrays(etf_prices, cfg)
    etf_n = int(rc.get("etf_max_positions", 5))
    etf = us_research.run_version(
        "etf", "ETF dips", live, ebars, esignals, eranks, cost_fn=costs, position_pct=100.0 / etf_n,
        max_positions=etf_n, min_trades=us_research.PASS["min_trades_etfs"], **common)
    log.info("etf: %s %s", "PASS" if etf.passed else "FAIL", "; ".join(etf.reasons))

    # --- For information only: other exit plans, and currency cost sensitivity
    other_plans = {}
    for key in ("L1", "L3"):
        o = us_research.run_version(key, plans[key].label, plans[key], bars, signals, ranks,
                                    cost_fn=costs, **stock_kw)
        other_plans[key] = o
    fx = {}
    for fx_pct in (0.0, 1.0):
        o = us_research.run_version(f"fx{fx_pct}", f"Dip rules alone, {fx_pct}% conversion", live, bars,
                                    signals, ranks, cost_fn=us_charges.cost_fn(cfg, fx_pct=fx_pct), **stock_kw)
        fx[fx_pct] = o

    choice, reason = us_research.recommend(outcomes)
    rupees = rupee_view(outcomes, spy, usdinr, capital, today)

    write_json(outcomes, etf, other_plans, fx, choice, reason, rupees, today)
    pd.DataFrame([dict(t, version=o.key) for o in outcomes + [etf] for t in o.trades]).to_csv(
        REPORTS / "us_trades.csv", index=False)
    write_report(outcomes, etf, other_plans, fx, choice, reason, rupees, today,
                 len(stock_prices), len(etf_prices), time.time() - started)

    print("\n=== SUMMARY ===")
    for o in outcomes + [etf]:
        line = "  ".join(f"{k} {num(p['curve']['cagr_pct'])}%/{num(p['curve']['max_drawdown_pct'])}% "
                         f"vs SPY {num(p['benchmark']['cagr_pct'])}%/{num(p['benchmark']['max_drawdown_pct'])}%"
                         for k, p in o.periods.items())
        print(f"{o.key:<11} {'PASS' if o.passed else 'FAIL':<5} {line}")
    print(f"US stocks: {choice or 'none passed'} - {reason}")
    print(f"ETFs: {'PASS' if etf.passed else 'FAIL'}")
    return 0


def rupee_view(outcomes, spy, usdinr, capital, today) -> dict:
    """2022+ growth in rupees, which is what an Indian investor actually earns."""
    out = {}
    start = research.TEST_START
    bench = research.benchmark_curve(spy, start, today, capital)
    for label, curve in [("SPY", bench)] + [(o.key, o.periods["2022+"]["equity"]) for o in outcomes]:
        inr = us_research.in_rupees(curve, usdinr)
        if len(inr) > 1:
            out[label] = curve_stats(inr, float(inr.iloc[0]))["cagr_pct"]
    if usdinr is not None and not usdinr.empty:
        rate = usdinr["close"]
        window = rate[rate.index >= pd.Timestamp(start)].dropna()
        if len(window) > 1:
            out["rupee_change_pct"] = (float(window.iloc[-1]) / float(window.iloc[0]) - 1) * 100.0
    return out


def _period_json(o):
    return {k: {kk: vv for kk, vv in p.items() if kk != "equity"} for k, p in o.periods.items()}


def write_json(outcomes, etf, other_plans, fx, choice, reason, rupees, today):
    def clean(obj):
        if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
            return None
        if isinstance(obj, dict):
            return {str(k): clean(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [clean(v) for v in obj]
        if isinstance(obj, date):
            return obj.isoformat()
        return obj

    payload = {
        "generated": today.isoformat(),
        "criteria": us_research.PASS,
        "stocks": {o.key: {"label": o.label, "passed": o.passed, "reasons": o.reasons,
                           "periods": _period_json(o)} for o in outcomes},
        "etfs": {"passed": etf.passed, "reasons": etf.reasons, "periods": _period_json(etf)},
        "other_exit_plans": {k: _period_json(o) for k, o in other_plans.items()},
        "fx_sensitivity": {str(k): _period_json(o) for k, o in fx.items()},
        "choice": choice, "reason": reason, "rupees_2022": rupees,
    }
    (REPORTS / "us_research.json").write_text(json.dumps(clean(payload), indent=2), encoding="utf-8")


def _rows(o):
    lines = []
    for name, p in o.periods.items():
        c, b, t = p["curve"], p["benchmark"], p["trades"]
        lines.append(f"| {name} | {num(c['cagr_pct'])}% | {num(c['max_drawdown_pct'])}% | "
                     f"{num(b['cagr_pct'])}% | {num(b['max_drawdown_pct'])}% | {t.get('trades') or 0} | "
                     f"{pct(t.get('win_rate'))} | {pct(c.get('exposure'))} |")
    return lines


HEADER = ["| Period | Growth a year | Worst fall | SPY growth | SPY worst fall | Trades | Win rate | Invested |",
          "|---|---|---|---|---|---|---|---|"]


def write_report(outcomes, etf, other_plans, fx, choice, reason, rupees, today, n_stocks, n_etfs, seconds):
    lines = [
        "# US stocks and ETFs: does the dip strategy work there?",
        "",
        f"Generated {today.isoformat()} in {seconds / 60:.0f} minutes from {n_stocks} S&P 500 / Nasdaq-100 "
        f"and {n_etfs} ETF price histories. The live dip rules and exit plan, unchanged from India: "
        "$100,000, 8% positions, at most 15, entry at the next session's open. Costs are Groww's US "
        "schedule (0.1% brokerage, at least $1, plus 18% GST, and SEC/FINRA fees on sells) plus 0.5% "
        "for converting rupees on every order. SPY is held with dividends reinvested and no costs.",
        "",
        "**Read with this caveat:** the stock lists are today's members. The S&P 500 adds companies "
        "after they have grown, so this is a list of past winners and every stock result is flattered, "
        "probably by more than in India. Tax is not deducted: gains held under 24 months are taxed at "
        "your slab rate, after that 12.5%, and the US withholds 25% of dividends.",
        "",
        "## Pass criteria, fixed before running",
        "",
        "In both 2014-21 and 2022 onward: growth above holding SPY, a worst fall no deeper than SPY's, "
        f"and at least {us_research.PASS['min_trades_stocks']} closed trades "
        f"({us_research.PASS['min_trades_etfs']} for ETFs).",
        "",
        f"## Decision: {'**' + choice + '**' if choice else '**no US stock signals**'}",
        "",
        reason,
        f"ETFs: **{'pass' if etf.passed else 'fail'}**.",
        "",
    ]
    for o in outcomes + [etf]:
        lines += [f"## {o.label}: {'PASS' if o.passed else 'fail'}", ""]
        if o.reasons:
            lines += ["Why it failed:", ""] + [f"- {r}" for r in o.reasons] + [""]
        lines += HEADER + _rows(o)
        reg = o.periods.get("2022+", {}).get("by_regime", {})
        if reg:
            lines += ["", "2022+ by market trend on the buy date: " + ", ".join(
                f"{k} {e['win_rate']:.0%} win, {e['avg_return_pct']:+.1f}% avg ({e['trades']})"
                for k, e in sorted(reg.items()))]
        lines.append("")

    if rupees:
        lines += ["## In rupees, 2022 onward", "",
                  "What an Indian investor actually earns includes the rupee's move against the dollar"
                  + (f" ({rupees['rupee_change_pct']:+.1f}% since January 2022)." if "rupee_change_pct" in rupees else "."),
                  ""]
        for key, value in rupees.items():
            if key != "rupee_change_pct":
                lines.append(f"- {key}: {num(value)}% a year in rupees")
        lines.append("")

    lines += ["## For information: other exit plans on US stocks (dip rules alone)", ""]
    for key, o in other_plans.items():
        lines += [f"**{key}: {o.label}**", ""] + HEADER + _rows(o) + [""]

    lines += ["## For information: currency conversion cost (dip rules alone)", ""]
    for fx_pct, o in fx.items():
        lines += [f"**{fx_pct}% per order**", ""] + HEADER + _rows(o) + [""]

    lines += [
        "## Sources", "",
        "- [Groww US stocks pricing](https://groww.in/pricing/us-stocks)",
        "- [S&P 500 constituents](https://en.wikipedia.org/wiki/List_of_S%26P_500_companies), "
        "[Nasdaq-100 constituents](https://en.wikipedia.org/wiki/Nasdaq-100)",
    ]
    (REPORTS / "us_research.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
