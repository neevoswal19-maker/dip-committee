"""Backtest short corrections: 3 falls x 3 windows x 2 universes.

The owner's version of correction buying: falls inside a year, measured from
the index's high of the last 3, 6 or 12 months. Rules and pass criteria in
src/strategy/correction.py (the "Short corrections" section), fixed before
running. Writes reports/short_correction_research.md, .json and
short_correction_trades.csv. Research only: no alerts, no database.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from jobs.correction_research import CAPITAL, num
from src import charges
from src.config import load_config
from src.data import nse, prices
from src.strategy import correction as cr
from src.strategy import swing

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("short_correction_research")

REPORTS = Path("reports")
YEARS = 25.0
DEPTHS = (5.0, 7.0, 10.0)
WINDOWS = ((63, "3 months"), (126, "6 months"), (252, "12 months"))


def survivors(frames, episode, sessions, costs):
    """Every stock with a year of history, bought at the same open, sold when the index trade ends."""
    if not sessions:
        return None
    trades = [t for t in (cr.trade(f, s, episode.trigger_date, float("inf"), stop_pct=None, max_hold=sessions)
                          for s, f in frames.items()
                          if int(f.index.searchsorted(episode.trigger_date, side="right")) >= cr.SHORT_MIN_HISTORY)
              if t is not None]
    return cr.basket(trades, costs, CAPITAL).get("net_pct")


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
    frames = {s: swing.adjust_for_dividends(r.value).dropna(subset=["open", "high", "low", "close"])
              for s, r in fetched.items()
              if r is not None and r.value is not None and len(r.value) > cr.SHORT_MIN_HISTORY}
    idx = prices.get_index_history("nifty500", years=YEARS).value.dropna(subset=["close"])
    log.info("Price histories: %d of %d; Nifty 500 %s to %s", len(frames), len(n500),
             idx.index[0].date(), idx.index[-1].date())
    cutoff = idx.index[max(0, len(idx) - cr.SHORT_HOLD)]    # triggers after this have not run six months
    span_years = (idx.index[-1] - idx.index[0]).days / 365.25

    costs = lambda side, qty, price: charges.estimate(side, qty, price, cfg=cfg).total  # noqa: E731
    universes = (("Nifty 100", {s: f for s, f in frames.items() if s in n100}), ("Nifty 500", frames))
    variants, trade_rows, control = [], [], {}
    for depth in DEPTHS:
        for window, wlabel in WINDOWS:
            eps = cr.rolling_episodes(idx["close"], depth, window)
            log.info("%.0f%% from the %s high: %d episodes", depth, wlabel, len(eps))
            picks = {e.trigger_date: {s: cr.selected_short(f, e, window) for s, f in frames.items()} for e in eps}
            for uname, ufr in universes:
                rows = []
                for e in eps:
                    chosen = {s: p for s, p in picks[e.trigger_date].items() if p and s in ufr}
                    trades = [t for t in (cr.trade(ufr[s], s, e.trigger_date, p["target"], stop_pct=None,
                                                   max_hold=cr.SHORT_HOLD) for s, p in chosen.items())
                              if t is not None]
                    b = cr.basket(trades, costs, CAPITAL)
                    it = cr.index_trade(idx["close"], idx.get("open", idx["close"]), e, max_hold=cr.SHORT_HOLD)
                    key = (uname, e.trigger_date, it["sessions"])
                    if key not in control:
                        control[key] = survivors(ufr, e, it["sessions"], costs)
                    rows.append({
                        "trigger": e.trigger_date.date().isoformat(), "peak": e.peak_date.date().isoformat(),
                        "index_fall_pct": e.index_drawdown * 100, "judged": e.trigger_date <= cutoff,
                        "trades": b.get("trades", 0), "basket_pct": b.get("net_pct"),
                        "target_rate": b.get("target_rate", 0.0), "avg_months": (b.get("avg_sessions") or 0) / 21,
                        "index_pct": it["return_pct"], "survivors_pct": control[key],
                    })
                    trade_rows += [{"variant": f"{depth:.0f}% {wlabel} {uname}", "trigger": e.trigger_date.date(),
                                    "symbol": t.symbol, "entry_date": t.entry_date.date(), "entry": round(t.entry, 2),
                                    "target": round(t.target, 2), "exit_date": t.exit_date.date(),
                                    "exit": round(t.exit, 2), "reason": t.reason, "sessions": t.sessions,
                                    "gross_pct": round(t.gross_pct, 2)} for t in trades]
                judged = [r for r in rows if r["judged"] and r["trades"] and r["index_pct"] is not None]
                ok, why = cr.judge(judged, cr.SHORT_PASS)
                gaps = [r for r in judged if r["survivors_pct"] is not None]
                variant = {
                    "depth": depth, "window": window, "window_label": wlabel, "universe": uname,
                    "passed": ok, "reasons": why, "episodes": rows, "judged": len(judged),
                    "per_year": len(eps) / span_years,
                    "basket_mean": float(np.mean([r["basket_pct"] for r in judged])) if judged else None,
                    "index_mean": float(np.mean([r["index_pct"] for r in judged])) if judged else None,
                    "edge_vs_index": float(np.mean([r["basket_pct"] - r["index_pct"] for r in judged]))
                    if judged else None,
                    "edge_vs_survivors": float(np.mean([r["basket_pct"] - r["survivors_pct"] for r in gaps]))
                    if gaps else None,
                    "beat_survivors": sum(r["basket_pct"] > r["survivors_pct"] for r in gaps),
                }
                variants.append(variant)
                log.info("%.0f%% %s %s: %s %s", depth, wlabel, uname, "PASS" if ok else "FAIL", "; ".join(why))

    # Fixed before running: among passing variants, the largest edge over buying every stock.
    passing = [v for v in variants if v["passed"]]
    choice = max(passing, key=lambda v: v["edge_vs_survivors"]) if passing else None

    pd.DataFrame(trade_rows).to_csv(REPORTS / "short_correction_trades.csv", index=False)
    (REPORTS / "short_correction_research.json").write_text(json.dumps(
        {"generated": date.today().isoformat(), "criteria": cr.SHORT_PASS, "variants": variants},
        indent=2, default=str), encoding="utf-8")
    write_report(variants, choice, idx, len(frames), time.time() - started)

    print("\n=== SUMMARY ===")
    for v in variants:
        print(f"{v['depth']:.0f}% {v['window_label']:<9} {v['universe']:<9}: {'PASS' if v['passed'] else 'FAIL'}  "
              f"{v['per_year']:.1f}/yr  vs index {num(v['edge_vs_index'])}  vs all {num(v['edge_vs_survivors'])}  "
              f"{'; '.join(v['reasons'])}")
    print("Choice:", "none passed" if not choice else
          f"{choice['depth']:.0f}% {choice['window_label']} {choice['universe']}")
    return 0


def write_report(variants, choice, idx, n_frames, seconds):
    decision = (f"**{choice['depth']:.0f}% fall from the {choice['window_label']} high, {choice['universe']}**"
                if choice else "**nothing passed**")
    lines = [
        "# Short market corrections: backtest",
        "",
        f"Generated {date.today().isoformat()} in {seconds / 60:.0f} minutes. Nifty 500 from {idx.index[0].date()} "
        f"to {idx.index[-1].date()}; {n_frames} stock histories (today's Nifty 500, dividend-adjusted).",
        "",
        "**Rules (src/strategy/correction.py, fixed before running):** a correction is the Nifty 500 closing 5%, "
        "7% or 10% below its highest close of the last 3, 6 or 12 months. It ends when the index closes back within "
        "half that fall of its window high; only then can the next one start. Buy every stock that fell further than the index from its "
        "own high over the same window, equal money each (Rs 5 lakh basket), at the next open; sell at that high "
        "or after six months. No stop. Groww delivery charges and slippage. The index trade buys at the same open "
        "and sells when the index regains its peak, or after six months, with no costs. Episodes can overlap.",
        "",
        "**Pass criteria:** at least 10 completed episodes; the basket beats the index trade in two-thirds of "
        "them and on average; at least 60% of trades reach their high within six months; and the basket beats "
        "buying every stock in the universe at the same moment, on average. That last check was added after "
        "the first correction study showed today's index members beat the index on their own (survivorship).",
        "",
        f"## Decision: {decision}",
        "",
        "| Fall | From the high of | Universe | Result | Episodes a year | Judged | Basket avg | Index avg | "
        "Beat index by | Beat every-stock basket by | Why it failed |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for v in variants:
        lines.append(
            f"| {v['depth']:.0f}% | last {v['window_label']} | {v['universe']} | {'PASS' if v['passed'] else 'fail'} | "
            f"{v['per_year']:.1f} | {v['judged']} | {num(v['basket_mean'])}% | {num(v['index_mean'])}% | "
            f"{num(v['edge_vs_index'])} | {num(v['edge_vs_survivors'])} ({v['beat_survivors']} of {v['judged']}) | "
            f"{'; '.join(v['reasons']) or '-'} |")
    for v in variants:
        lines += ["", f"### {v['depth']:.0f}% fall from the {v['window_label']} high, {v['universe']}", "",
                  "| Triggered | Index fall | Stocks | Basket | Index trade | Every stock | Reached high | "
                  "Avg months | Judged |", "|---|---|---|---|---|---|---|---|---|"]
        for r in v["episodes"]:
            lines.append(f"| {r['trigger']} | {num(r['index_fall_pct'])}% | {r['trades']} | {num(r['basket_pct'])}% | "
                         f"{num(r['index_pct'])}% | {num(r['survivors_pct'])}% | {r['target_rate']:.0%} | "
                         f"{r['avg_months']:.0f} | {'yes' if r['judged'] else 'in progress'} |")
    (REPORTS / "short_correction_research.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
