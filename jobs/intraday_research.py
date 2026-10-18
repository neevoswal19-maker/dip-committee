"""Backtest the intraday ORB + VWAP pullback rules on every stored session.

Fetches Yahoo's last 60 sessions of 5-minute bars for NIFTYBEES and BANKBEES,
adds whatever the live job has stored (and stores the fresh bars, so history
grows), runs src/intraday/orb.py over every session after the first, and
scores the paper trades logged so far. Writes:

  reports/intraday_research.md     the report
  reports/intraday_research.json   machine-readable results
  reports/intraday_trades.csv      every backtest trade

Works without a database (Yahoo's 60 sessions only).
"""

from __future__ import annotations

import json
import logging
import math
import os
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from src.config import load_config
from src.intraday import data, orb

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("intraday_research")

REPORTS = Path("reports")


def num(x, digits=2) -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "-"
    return f"{x:,.{digits}f}"


def pct(x) -> str:
    return "-" if x is None else f"{x:.0%}"


def main() -> int:
    started = time.time()
    cfg = load_config()
    REPORTS.mkdir(exist_ok=True)
    names = data.symbols(cfg)
    capital = float(cfg.get("intraday.capital", 500000))

    fresh = data.fetch(names, period="60d")
    log.info("Yahoo: %s", {n: f"{len(f)} bars, {f.index.min():%d %b} to {f.index.max():%d %b}" for n, f in fresh.items()})

    bars, paper = fresh, []
    if os.environ.get("DATABASE_URL"):
        from sqlalchemy import select

        from src import db
        db.init_db()
        log.info("Stored %d new bars", data.store(fresh))
        bars = data.merge(data.load(names), fresh)
        with db.connection() as conn:
            paper = [dict(r._mapping) for r in conn.execute(
                select(db.intraday_trades).where(db.intraday_trades.c.mode == "paper"))]
        for row in paper:
            row["closed"] = bool(row.get("closed"))

    results, events = orb.backtest(bars, cfg)
    sessions = {n: len({ts.date() for ts in f.index}) for n, f in bars.items()}
    card = orb.scorecard(results, capital)
    by_symbol = {n: orb.scorecard([r for r in results if r["symbol"] == n], capital) for n in names}
    by_side = {s: orb.scorecard([r for r in results if r["side"] == s], capital) for s in ("LONG", "SHORT")}
    kinds = pd.Series([e.kind for e in events]).value_counts().to_dict() if events else {}
    paper_card = orb.scorecard(paper, capital)
    verdict, why = orb.judge(paper_card)
    log.info("Backtest: %s", card)

    pd.DataFrame(results).to_csv(REPORTS / "intraday_trades.csv", index=False)
    payload = {"generated": date.today().isoformat(), "sessions": sessions, "backtest": card,
               "by_symbol": by_symbol, "by_side": by_side, "events": kinds, "paper": paper_card,
               "paper_verdict": verdict, "paper_reasons": why, "criteria": orb.PASS}
    (REPORTS / "intraday_research.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    write_report(results, card, by_symbol, by_side, kinds, sessions, paper_card, verdict, why,
                 time.time() - started)

    print("\n=== SUMMARY ===")
    print(f"Sessions: {sessions}")
    print(f"Backtest: {card.get('trades', 0)} trades, win {pct(card.get('win_rate'))}, avg {num(card.get('avg_r'))}R, "
          f"PF {num(card.get('profit_factor'))}, net Rs {num(card.get('net'), 0)}, worst DD {num(card.get('max_drawdown_pct'), 1)}%")
    for name, s in list(by_symbol.items()) + list(by_side.items()):
        print(f"  {name:<10} {s.get('trades', 0)} trades, win {pct(s.get('win_rate'))}, PF {num(s.get('profit_factor'))}, "
              f"net Rs {num(s.get('net'), 0)}")
    print(f"Events: {kinds}")
    print(f"Paper: {paper_card.get('trades', 0)} trades; verdict {verdict} {why}")
    return 0


def _row(label: str, s: dict) -> str:
    if not s.get("trades"):
        return f"| {label} | 0 | - | - | - | - | - |"
    return (f"| {label} | {s['trades']} | {pct(s['win_rate'])} | {num(s['avg_r'])}R | {num(s['profit_factor'])} | "
            f"Rs {num(s['net'], 0)} | {num(s['max_drawdown_pct'], 1)}% |")


def write_report(results, card, by_symbol, by_side, kinds, sessions, paper_card, verdict, why, seconds):
    header = ["| | Trades | Win rate | Avg R | Profit factor | Net after costs | Worst drawdown |",
              "|---|---|---|---|---|---|---|"]
    lines = [
        "# Intraday ORB + VWAP/20-EMA pullback: backtest",
        "",
        f"Generated {date.today().isoformat()} in {seconds:.0f} seconds. Sessions of 5-minute bars: "
        + ", ".join(f"{n} {k}" for n, k in sessions.items())
        + ". Rules in src/intraday/orb.py, fixed before running. Rs 5 lakh, Rs 5,000 risked per trade, exposure "
        "capped at Rs 7.5 lakh (Rs 1.5 lakh margin at 5x), Groww intraday charges and 0.02% slippage.",
        "",
        "**A first look, not a verdict.** About 60 sessions is too few to tell an edge from luck: one good "
        "week moves every figure. The verdict comes from 40 paper trades judged against the bar below, "
        "and this backtest grows as the live job stores each day's bars.",
        "",
        "## All trades", "",
    ] + header + [_row("Both ETFs, long and short", card)] + [
        "", "## By instrument and side", "",
    ] + header + [_row(n, s) for n, s in by_symbol.items()] + [_row(s, v) for s, v in by_side.items()] + [
        "", "## How setups ended", "",
        ", ".join(f"{k}: {v}" for k, v in sorted(kinds.items())) or "none",
        "",
        "Armed = a breakout bar met every condition; entry = the pullback triggered; expired = no trigger "
        "within 6 candles; cancelled = RSI crossed 50 or a pullback bar out-traded the breakout; skipped = "
        "the opening range was too wide.",
        "",
        "## Paper trades so far", "",
    ] + header + [_row("Paper", paper_card)] + [
        "",
        f"The bar, fixed in advance: at least {orb.PASS['min_trades']} trades, profit factor at least "
        f"{orb.PASS['min_profit_factor']}, a positive average trade after costs, and a worst drawdown no "
        f"deeper than {orb.PASS['max_drawdown_pct']:.0f}% of capital. Status: "
        + ("**pass**" if verdict else "**fail**" if verdict is False else "not judged yet")
        + (f" ({'; '.join(why)})." if why else "."),
    ]
    if results:
        lines += ["", "## Every backtest trade", "",
                  "| Day | ETF | Side | Entry | Stop | Units | Exit | Net | R |", "|---|---|---|---|---|---|---|---|---|"]
        for r in results:
            lines.append(f"| {r['day']} | {r['symbol']} | {r['side']} | {num(r['entry'])} | {num(r['stop'])} | "
                         f"{r['units']:,} | {r['exit_reason'] or 'open'} | Rs {num(r['net'], 0)} | {num(r['r'])} |")
    (REPORTS / "intraday_research.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
