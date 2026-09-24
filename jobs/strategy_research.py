"""Run the strategy research and write the report.

Tests every published swing rule against nine stop/target plans on the Nifty
50, confirms the chosen plan on later years and on the Nifty Next 50, and
compares three exit plans for the long-term dip strategy. Writes:

  reports/strategy_research.md    the report
  reports/strategy_research.json  machine-readable results, for config
  reports/swing_trades.csv        every trade behind the swing numbers
  reports/long_term_trades.csv    every trade behind the long-term numbers

Runs on GitHub Actions (.github/workflows/strategy-research.yml). It needs
about fifteen years of daily prices for ~550 stocks, fetched from Yahoo.
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

from src.config import load_config
from src.data import nse, prices
from src.strategy import research, swing
from src.strategy.exit_policies import long_term_plans
from src.strategy.simulator import curve_stats, trade_stats

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("strategy_research")

REPORTS = Path("reports")


def fetch(universe: list, years: float) -> dict[str, pd.DataFrame]:
    results = prices.get_price_history_batch(universe, years=years)
    return {s: r.value for s, r in results.items() if r is not None and r.usable and r.value is not None}


def pct(x) -> str:
    return "-" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:.0%}"


def num(x, digits=2) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "-"
    if isinstance(x, float) and math.isinf(x):
        return "inf"
    return f"{x:.{digits}f}"


def stats_row(label: str, s: dict) -> str:
    if not s.get("trades"):
        return f"| {label} | 0 | - | - | - | - | - |"
    return (f"| {label} | {s['trades']} | {pct(s['win_rate'])} | {num(s['avg_win_pct'])}% | "
            f"{num(s['avg_loss_pct'])}% | {num(s['profit_factor'])} | {num(s['expectancy_pct'])}% |")


def main() -> int:
    parser = argparse.ArgumentParser(description="Swing and long-term exit research")
    parser.add_argument("--years", type=float, default=15.0)
    parser.add_argument("--long-term-limit", type=int, default=500,
                        help="how many Nifty 500 stocks the long-term test uses")
    parser.add_argument("--skip-long-term", action="store_true")
    args = parser.parse_args()

    started = time.time()
    cfg = load_config()
    today = date.today()
    REPORTS.mkdir(exist_ok=True)

    # --- Universes
    n50 = nse.get_universe("NIFTY 50").value or []
    n100 = nse.get_universe("NIFTY 100").value or []
    n50_symbols = {s.symbol for s in n50}
    next50 = [s for s in n100 if s.symbol not in n50_symbols]
    log.info("Nifty 50: %d, Nifty Next 50: %d", len(n50), len(next50))
    if len(n50) < 40:
        log.error("Could not load the Nifty 50 universe")
        return 1

    # --- Prices
    log.info("Fetching prices")
    p50 = fetch(n50, args.years)
    pN = fetch(next50, args.years)
    nifty_index = prices.get_index_history("nifty50", years=args.years).value
    nifty500_index = prices.get_index_history("nifty500", years=args.years).value
    regime_at = research.regime_lookup(nifty500_index)

    f50 = research.swing_features(p50)
    fN = research.swing_features(pN)
    b50, bN = research.to_bars(f50), research.to_bars(fN)
    log.info("Usable histories: %d Nifty 50, %d Next 50", len(b50), len(bN))

    sanity = research.sanity_check_index(nifty_index, today, cfg)
    log.info("Sanity (RSI(2) on the Nifty 50 index, no costs): %s", sanity)

    # --- Swing rules
    verdicts = []
    for key, rule in swing.RULES.items():
        log.info("Rule %s", key)
        verdict = research.evaluate_rule(rule, (f50, b50), (fN, bN), end=today, cfg=cfg,
                                         regime_at=regime_at, nifty_index=nifty_index)
        log.info("  %s: %s  %s", key, "PASS" if verdict.passed else "FAIL",
                 "; ".join(verdict.reasons) or "all criteria met")
        verdicts.append(verdict)

    # --- Long-term exits
    long_term = {}
    lt_choice, lt_reason = "L1", "Long-term test skipped."
    if not args.skip_long_term:
        n500 = (nse.get_universe("NIFTY 500").value or [])[: args.long_term_limit]
        log.info("Fetching %d Nifty 500 histories for the long-term test", len(n500))
        p500 = fetch(n500, args.years)
        bars, signals, ranks = research.long_term_arrays(p500, cfg)
        log.info("Long-term: %d usable histories", len(bars))
        for key, plan in long_term_plans(cfg).items():
            entry = {"label": plan.label}
            for label, start, end in (("train", research.TRAIN[0], research.TRAIN[1]),
                                      ("test", research.TEST_START, today)):
                result = research.run_long_term(plan, bars, signals, ranks, start=start,
                                                end=end, cfg=cfg, regime_at=regime_at)
                entry[f"{label}_curve"] = curve_stats(result.equity, result.initial_capital)
                entry[f"{label}_curve"]["exposure"] = result.exposure
                entry[f"{label}_trades"] = research._brief(trade_stats(result.trades))
                entry[f"{label}_rows"] = [t.to_row() for t in result.trades]
                entry[f"{label}_exits"] = trade_stats(result.trades).get("by_exit", {})
            long_term[key] = entry
            log.info("  %s test: %s", key, entry["test_curve"])
        lt_choice, lt_reason = research.choose_long_term(long_term)
        bench_lt = curve_stats(research.benchmark_curve(nifty_index, research.TEST_START, today, 1_000_000.0),
                               1_000_000.0)
    else:
        bench_lt = {}

    # --- Outputs
    write_json(verdicts, sanity, long_term, lt_choice, lt_reason, today)
    write_csv(verdicts, long_term)
    write_report(verdicts, sanity, long_term, lt_choice, lt_reason, bench_lt, today,
                 len(b50), len(bN), time.time() - started)

    passed = [v.rule.key for v in verdicts if v.passed]
    print("\n=== SUMMARY ===")
    for v in verdicts:
        print(f"{v.rule.key:<10} {'PASS' if v.passed else 'FAIL':<5} {v.plan.label:<34} "
              f"train {pct(v.train.get('win_rate'))}/{num(v.train.get('profit_factor'))}  "
              f"test {pct(v.test.get('win_rate'))}/{num(v.test.get('profit_factor'))}  "
              f"next50 {pct(v.next50.get('win_rate'))}/{num(v.next50.get('profit_factor'))}")
    print(f"Swing strategies passing: {passed or 'none'}")
    print(f"Long-term exit plan: {lt_choice} - {lt_reason}")
    return 0


def write_json(verdicts, sanity, long_term, lt_choice, lt_reason, today):
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
        "criteria": research.PASS,
        "sanity_check": sanity,
        "swing": {
            v.rule.key: {
                "name": v.rule.name, "passed": v.passed, "reasons": v.reasons,
                "plan": {"stop_atr": v.plan.stop_atr, "target_atr": v.plan.target_atr,
                         "max_hold": v.plan.max_hold, "label": v.plan.label},
                "train": {k: v.train.get(k) for k in ("trades", "win_rate", "avg_win_pct", "avg_loss_pct",
                                                      "profit_factor", "expectancy_pct")},
                "test": {k: v.test.get(k) for k in ("trades", "win_rate", "avg_win_pct", "avg_loss_pct",
                                                    "profit_factor", "expectancy_pct", "by_year", "by_regime")},
                "test_same_close": {k: v.test_same_close.get(k) for k in ("trades", "win_rate", "profit_factor",
                                                                          "expectancy_pct")},
                "next50": {k: v.next50.get(k) for k in ("trades", "win_rate", "profit_factor", "expectancy_pct")},
                "portfolio": v.portfolio, "benchmark": v.benchmark,
            }
            for v in verdicts
        },
        "long_term": {
            "choice": lt_choice, "reason": lt_reason,
            "plans": {k: {kk: vv for kk, vv in e.items() if not kk.endswith("_rows")}
                      for k, e in long_term.items()},
        },
    }
    (REPORTS / "strategy_research.json").write_text(json.dumps(clean(payload), indent=2), encoding="utf-8")


def write_csv(verdicts, long_term):
    rows = [dict(r, rule=v.rule.key, plan=v.plan.label) for v in verdicts for r in v.trades]
    pd.DataFrame(rows).to_csv(REPORTS / "swing_trades.csv", index=False)
    lt_rows = [dict(r, plan=k, period=p) for k, e in long_term.items()
               for p in ("train", "test") for r in e.get(f"{p}_rows", [])]
    pd.DataFrame(lt_rows).to_csv(REPORTS / "long_term_trades.csv", index=False)


def write_report(verdicts, sanity, long_term, lt_choice, lt_reason, bench_lt, today,
                 n50, nN, seconds):
    lines = [
        "# Strategy research",
        "",
        f"Generated {today.isoformat()} in {seconds / 60:.0f} minutes. {n50} Nifty 50 and {nN} Nifty Next 50 "
        "price histories. Every figure is after Groww charges (brokerage, STT both sides, exchange, "
        "stamp duty, DP, GST) at Rs 50,000 per trade plus 0.05% slippage on market fills. Entry at the "
        "next session's open unless stated. A trade is a win only if it made money after costs.",
        "",
        "**Read with this caveat:** both universes are today's index members, so stocks that dropped "
        "out are missing and every result is flattered. Profits would also be taxed as short-term "
        "capital gains (20%), which is not deducted here.",
        "",
        "## Sanity check",
        "",
        "RSI(2) on the Nifty 50 index itself, before costs, 2014 onward - published tests on broad "
        "indices find win rates well above 60%:",
        "",
        f"{sanity.get('trades', 0)} trades, win rate {pct(sanity.get('win_rate'))}, "
        f"average trade {num(sanity.get('expectancy_pct'))}%, profit factor {num(sanity.get('profit_factor'))}.",
        "",
        "## Swing strategies",
        "",
        "Criteria, fixed before running: win rate at least 60% and profit factor at least 1.3 in both "
        "2014-21 and 2022 onward; on the Nifty Next 50 at least 55% and above 1.1; and a 5-slot "
        "portfolio falling no further than simply holding the Nifty 50.",
        "",
        "| Rule | Result | Stop / target (chosen on 2014-21) | Win 2014-21 | Win 2022+ | PF 2022+ | "
        "Avg trade 2022+ | Next 50 win | Portfolio CAGR / worst fall | Nifty 50 CAGR / worst fall |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for v in verdicts:
        lines.append(
            f"| {v.rule.name} | **{'PASS' if v.passed else 'fail'}** | {v.plan.label} | "
            f"{pct(v.train.get('win_rate'))} | {pct(v.test.get('win_rate'))} | "
            f"{num(v.test.get('profit_factor'))} | {num(v.test.get('expectancy_pct'))}% | "
            f"{pct(v.next50.get('win_rate'))} | "
            f"{num(v.portfolio.get('cagr_pct'), 1)}% / {num(v.portfolio.get('max_drawdown_pct'), 1)}% | "
            f"{num(v.benchmark.get('cagr_pct'), 1)}% / {num(v.benchmark.get('max_drawdown_pct'), 1)}% |"
        )

    for v in verdicts:
        lines += [
            "", f"### {v.rule.name} - {'PASS' if v.passed else 'fail'}", "",
            f"*Entry:* {v.rule.entry_text}. *Exit:* {v.rule.exit_text}. *Source:* {v.rule.source}.",
            "",
        ]
        if v.reasons:
            lines += ["Why it failed:", ""] + [f"- {r}" for r in v.reasons] + [""]
        lines += [
            "| Sample | Trades | Win rate | Avg win | Avg loss | Profit factor | Avg trade |",
            "|---|---|---|---|---|---|---|",
            stats_row("2014-21 (plan chosen here)", v.train),
            stats_row("2022+ next-open", v.test),
            stats_row("2022+ same-close", v.test_same_close),
            stats_row("Nifty Next 50, all years", v.next50),
            "",
            "All nine plans on 2014-21:",
            "",
            "| Plan | Trades | Win rate | Profit factor | Avg trade |",
            "|---|---|---|---|---|",
        ]
        for row in v.grid:
            lines.append(f"| {row['plan']} | {row.get('trades') or 0} | {pct(row.get('win_rate'))} | "
                         f"{num(row.get('profit_factor'))} | {num(row.get('expectancy_pct'))}% |")
        by_year = v.test.get("by_year", {}) | v.train.get("by_year", {})
        if by_year:
            lines += ["", "Win rate by year: " + ", ".join(
                f"{y} {e['win_rate']:.0%} ({e['trades']})" for y, e in sorted(by_year.items()))]
        by_regime = v.test.get("by_regime", {})
        if by_regime:
            lines += ["", "2022+ by market regime on the signal day: " + ", ".join(
                f"{k} {e['win_rate']:.0%} win, {e['avg_return_pct']:+.2f}% avg ({e['trades']})"
                for k, e in sorted(by_regime.items()))]
        by_exit = v.test.get("by_exit", {})
        if by_exit:
            lines += ["", "2022+ by exit: " + ", ".join(
                f"{k} {e['trades']} ({e['avg_return_pct']:+.2f}% avg)" for k, e in sorted(by_exit.items()))]

    if long_term:
        lines += [
            "", "## Long-term exit plans", "",
            "The live dip rules on the Nifty 500 (15 positions of 8% of Rs 10 lakh, checked weekly, "
            "entry at the next open, costs included). Delivery confirmation and the committee are not "
            "applied - they cannot be reconstructed historically.",
            "",
            "| Plan | CAGR 2014-21 | Worst fall 2014-21 | CAGR 2022+ | Worst fall 2022+ | Trades 2022+ | Win rate 2022+ |",
            "|---|---|---|---|---|---|---|",
        ]
        for key, e in long_term.items():
            lines.append(
                f"| {key}: {e['label']} | {num(e['train_curve']['cagr_pct'], 1)}% | "
                f"{num(e['train_curve']['max_drawdown_pct'], 1)}% | {num(e['test_curve']['cagr_pct'], 1)}% | "
                f"{num(e['test_curve']['max_drawdown_pct'], 1)}% | {e['test_trades'].get('trades') or 0} | "
                f"{pct(e['test_trades'].get('win_rate'))} |"
            )
        if bench_lt:
            lines += ["", f"Holding the Nifty 50 from 2022: {num(bench_lt.get('cagr_pct'), 1)}% a year, "
                          f"worst fall {num(bench_lt.get('max_drawdown_pct'), 1)}%."]
        lines += ["", f"**Decision: {lt_choice}.** {lt_reason}"]

    lines += [
        "", "## Sources", "",
        "- Connors & Alvarez, *Short Term Trading Strategies That Work* (2008): RSI(2), cumulative RSI, Double 7s",
        "- Connors, *High Probability ETF Trading* (2009): TPS",
        "- [RSI(2) on 150 S&P 500 stocks, survivorship-free: 64.7% win, profit factor 1.01]"
        "(https://www.elitetrader.com/et/threads/backtested-a-mean-reversion-rsi-2-pullback-strategy-on-150-s-p500-names.390710/)",
        "- [Mean reversion on the Nifty 50](https://delphicalpha.substack.com/p/does-mean-reversion-work-on-the-nifty)",
        "- [IBS strategies](https://www.quantifiedstrategies.com/ibs-internal-bar-strength-indicator-strategies/)",
        "- [Double 7s](https://www.quantifiedstrategies.com/larry-connors-double-seven-strategy-does-it-still-work/)",
        "- Chuck LeBeau, chandelier exit",
    ]
    (REPORTS / "strategy_research.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
