"""Backtest correction buying: 3 triggers x 2 universes x 2 exits.

See src/strategy/correction.py for the rules and the pass criteria, fixed
before running. Writes reports/correction_research.md, .json and
correction_trades.csv. Research only: no alerts, no database.
"""

from __future__ import annotations

import json
import logging
import math
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from src import charges
from src.config import load_config
from src.data import nse, prices
from src.strategy import correction as cr
from src.strategy import swing

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("correction_research")

REPORTS = Path("reports")
YEARS = 25.0          # stocks need 2 years of history before the 2007-08 triggers
DEPTHS = (10.0, 15.0, 20.0)
EXITS = (("E1", "old high or 2 years, no stop", None), ("E2", "old high or 2 years, -25% stop", 25.0))
CAPITAL = 500000.0


def num(x, digits=1) -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "-"
    return f"{x:+.{digits}f}"


def main() -> int:
    started = time.time()
    cfg = load_config()
    REPORTS.mkdir(exist_ok=True)

    n500 = nse.get_universe("NIFTY 500").value or []
    n100 = {s.symbol for s in (nse.get_universe("NIFTY 100").value or [])}
    if len(n500) < 400 or len(n100) < 80:
        log.error("Could not load the index lists")
        return 1
    fetched = prices.get_price_history_batch(n500, years=YEARS)
    frames = {s: swing.adjust_for_dividends(r.value) for s, r in fetched.items()
              if r is not None and r.value is not None and len(r.value) > cr.MIN_HISTORY}
    log.info("Price histories: %d of %d", len(frames), len(n500))

    index_name, idx = "Nifty 500", prices.get_index_history("nifty500", years=YEARS).value
    if idx is None or idx.empty or idx.index[0] > pd.Timestamp("2008-01-31"):
        index_name, idx = "Nifty 50", prices.get_index_history("nifty50", years=YEARS).value
    idx = idx.dropna(subset=["close"])
    log.info("Episodes measured on the %s, %s to %s", index_name, idx.index[0].date(), idx.index[-1].date())
    cutoff = idx.index[max(0, len(idx) - cr.MAX_HOLD)]    # triggers after this have not run two years

    costs = lambda side, qty, price: charges.estimate(side, qty, price, cfg=cfg).total  # noqa: E731
    universes = (("Nifty 100", {s: f for s, f in frames.items() if s in n100}), ("Nifty 500", frames))

    variants, trade_rows = [], []
    for depth in DEPTHS:
        eps = cr.episodes(idx["close"], depth)
        log.info("%.0f%% trigger: %d episodes: %s", depth, len(eps), [str(e.trigger_date.date()) for e in eps])
        picks = {e.trigger_date: {s: cr.selected(f, e) for s, f in frames.items()} for e in eps}
        for uname, ufr in universes:
            for ekey, elabel, stop in EXITS:
                rows = []
                for e in eps:
                    chosen = {s: p for s, p in picks[e.trigger_date].items() if p and s in ufr}
                    trades = [t for t in (cr.trade(ufr[s], s, e.trigger_date, p["target"], stop_pct=stop)
                                          for s, p in chosen.items()) if t is not None]
                    b = cr.basket(trades, costs, CAPITAL)
                    it = cr.index_trade(idx["close"], idx.get("open", idx["close"]), e)
                    rows.append({
                        "trigger": e.trigger_date.date().isoformat(), "peak": e.peak_date.date().isoformat(),
                        "index_fall_pct": e.index_drawdown * 100, "judged": e.trigger_date <= cutoff,
                        "trades": b.get("trades", 0), "basket_pct": b.get("net_pct"),
                        "per_year_pct": b.get("per_year_pct"), "target_rate": b.get("target_rate", 0.0),
                        "stopped": b.get("stopped", 0), "open": b.get("open", 0),
                        "avg_months": (b.get("avg_sessions") or 0) / 21, "index_pct": it["return_pct"],
                        "index_months": it["sessions"] / 21, "index_reached": it["reached"],
                    })
                    trade_rows += [{"variant": f"{depth:.0f}% {uname} {ekey}", "trigger": e.trigger_date.date(),
                                    "symbol": t.symbol, "entry_date": t.entry_date.date(), "entry": round(t.entry, 2),
                                    "target": round(t.target, 2), "exit_date": t.exit_date.date(),
                                    "exit": round(t.exit, 2), "reason": t.reason, "sessions": t.sessions,
                                    "gross_pct": round(t.gross_pct, 2)} for t in trades]
                judged = [r for r in rows if r["judged"] and r["trades"] and r["index_pct"] is not None]
                ok, why = cr.judge(judged)
                score = float(np.mean([r["per_year_pct"] for r in judged])) if judged else None
                variants.append({"depth": depth, "universe": uname, "exit": ekey, "exit_label": elabel,
                                 "passed": ok, "reasons": why, "score": score, "episodes": rows})
                log.info("%.0f%% %s %s: %s %s", depth, uname, ekey, "PASS" if ok else "FAIL", "; ".join(why))

    passing = [v for v in variants if v["passed"]]
    choice = max(passing, key=lambda v: v["score"]) if passing else None

    pd.DataFrame(trade_rows).to_csv(REPORTS / "correction_trades.csv", index=False)
    (REPORTS / "correction_research.json").write_text(json.dumps(
        {"generated": date.today().isoformat(), "index": index_name, "criteria": cr.PASS, "variants": variants,
         "choice": None if not choice else f"{choice['depth']:.0f}% {choice['universe']} {choice['exit']}"},
        indent=2, default=str), encoding="utf-8")
    write_report(variants, choice, index_name, idx, len(frames), time.time() - started)

    print("\n=== SUMMARY ===")
    for v in variants:
        print(f"{v['depth']:.0f}% {v['universe']:<9} {v['exit']}: {'PASS' if v['passed'] else 'FAIL'}  "
              f"{'; '.join(v['reasons'])}")
    print("Choice:", "none passed" if not choice else f"{choice['depth']:.0f}% {choice['universe']} {choice['exit']}")
    return 0


def write_report(variants, choice, index_name, idx, n_frames, seconds):
    decision = (f"**{choice['depth']:.0f}% fall, {choice['universe']}, {choice['exit_label']}**" if choice
                else "**nothing passed**")
    lines = [
        "# Buying market corrections: backtest",
        "",
        f"Generated {date.today().isoformat()} in {seconds / 60:.0f} minutes. Episodes on the {index_name}, "
        f"{idx.index[0].date()} to {idx.index[-1].date()}; {n_frames} stock histories (today's Nifty 500, "
        "dividend-adjusted). Rules in src/strategy/correction.py, fixed before running: buy every stock that "
        "fell further than the index from its pre-correction high, equal money each (Rs 5 lakh basket), at "
        "the next open after the trigger; sell at that old high, or after two years (E2 also at -25%). "
        "Groww delivery charges and slippage. The benchmark buys the index at the same open and sells when "
        "it regains its peak, or after two years, with no costs.",
        "",
        "**Pass criteria:** at least 4 completed episodes; the basket beats the index trade in two-thirds of "
        "them and on average; at least 60% of trades reach their old high within two years. Episodes whose "
        "two years have not run out are shown but not judged.",
        "",
        "**Read with this caveat:** today's index members only, so companies that never recovered and "
        "dropped out are missing, which flatters the target rate most of all. Returns are before tax.",
        "",
        f"## Decision: {decision}",
        "",
        "| Trigger | Universe | Exit | Result | Avg return a year | Why it failed |",
        "|---|---|---|---|---|---|",
    ]
    for v in variants:
        lines.append(f"| {v['depth']:.0f}% | {v['universe']} | {v['exit']} | {'PASS' if v['passed'] else 'fail'} | "
                     f"{num(v['score'])}% | {'; '.join(v['reasons']) or '-'} |")
    for v in variants:
        lines += ["", f"### {v['depth']:.0f}% fall, {v['universe']}, {v['exit_label']}", "",
                  "| Triggered | Index fall | Stocks | Basket | Index trade | Reached old high | Stopped | "
                  "Avg months | Judged |", "|---|---|---|---|---|---|---|---|---|"]
        for r in v["episodes"]:
            lines.append(f"| {r['trigger']} | {num(r['index_fall_pct'])}% | {r['trades']} | {num(r['basket_pct'])}% | "
                         f"{num(r['index_pct'])}% | {r['target_rate']:.0%} | {r['stopped']} | "
                         f"{r['avg_months']:.0f} | {'yes' if r['judged'] else 'in progress'} |")
    (REPORTS / "correction_research.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
