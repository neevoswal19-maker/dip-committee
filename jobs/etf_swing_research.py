"""Backtest the ETF relative-strength swing systems (2A, 2B and the portfolio, 3).

Rules: src/strategy/etf_swing.py. Two datasets:

  long history   2014 on, on stand-ins: GOLDBEES, silver futures in rupees
                 (SI=F x USDINR) and the NSE sector indices, against the Nifty 50
                 index. Index stand-ins carry no volume, so the volume-contraction
                 check is skipped there.
  real ETFs      the actual ETFs from whenever each listed (SILVERBEES 2022,
                 sector ETFs 2020-24), against NIFTYBEES, with the volume check.

Pass criteria, fixed before running, judged on the long-history dataset:
  2A and 2B, each on its own: in both 2014-21 and 2022+, at least 15 trades,
  profit factor at least 1.3 and a positive average trade after costs.
  3, the portfolio: in both periods, growth above holding NIFTYBEES (dividends
  reinvested) and a worst fall no deeper.

Writes reports/etf_swing_research.md, .json and etf_swing_trades.csv.
Research only: no alerts, no database.
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

import pandas as pd

from src.config import load_config
from src.data import prices
from src.data.provider import StockIdentity
from src.strategy import etf_swing as es
from src.strategy import research, swing
from src.strategy.simulator import NEXT_OPEN, Bars, curve_stats, simulate, trade_stats

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("etf_swing_research")

REPORTS = Path("reports")
YEARS = 16.0
PERIODS = (("2014-21", date(2014, 1, 1), date(2021, 12, 31)), ("2022+", date(2022, 1, 1), None))
PASS = {"min_trades": 15, "min_profit_factor": 1.3}


def num(x, digits=1) -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "-"
    return f"{x:.{digits}f}"


def pct(x) -> str:
    return "-" if x is None else f"{x:.0%}"


#: Sector index symbols Yahoo has used; the first with data wins.
INDEX_ALIASES = {
    "^CNXMETAL": ["^CNXMETAL", "NIFTY_METAL.NS", "^NIFTYMETAL"],
    "^CNXAUTO": ["^CNXAUTO", "NIFTY_AUTO.NS", "^NIFTYAUTO"],
    "^CNXPSUBANK": ["^CNXPSUBANK", "NIFTY_PSU_BANK.NS", "^NIFTYPSUBANK"],
}


def in_rupees(symbol: str) -> pd.DataFrame | None:
    """A dollar commodity future valued in rupees each day; no volume."""
    metal = history(symbol)
    rupee = history("USDINR=X")
    if metal is None or rupee is None:
        return None
    rate = rupee["close"].reindex(metal.index).ffill()
    out = metal[["open", "high", "low", "close"]].mul(rate, axis=0).dropna()
    out["volume"] = float("nan")
    return out


def history(symbol: str, members: dict[str, pd.DataFrame] | None = None) -> pd.DataFrame | None:
    if symbol.endswith("*USDINR"):
        return in_rupees(symbol.split("*")[0])
    for candidate in INDEX_ALIASES.get(symbol, [symbol]):
        frame = prices.get_index_history(candidate, years=YEARS).value
        if frame is not None and not frame.empty:
            return swing.adjust_for_dividends(es.repair_splits(frame))
    if members:
        log.info("No Yahoo history for %s; using an equal-weight index of %d members", symbol, len(members))
        return es.composite(members)
    log.warning("No history for %s", symbol)
    return None


def build(assets: dict[str, tuple[str, str, bool]], nifty_close: pd.Series, constituents: dict,
          *, use_volume: bool) -> dict[str, dict]:
    """name -> {bars, signal, rank, kind, gated} for every asset with data."""
    regime = es.above_sma50(nifty_close)
    out = {}
    for name, (symbol, kind, gated) in assets.items():
        frame = history(symbol, constituents.get(name) if kind == "sector" else None)
        if frame is None or len(frame) < 120:
            continue
        if not use_volume:
            frame = frame.assign(volume=float("nan"))
        f = es.features(frame, nifty_close)
        if kind == "sector":
            members = constituents.get(name, {})
            ok = es.breadth(members, f.index)
            sig, rank = es.signals(f, breadth_ok=ok, regime_ok=regime if gated else None)
        else:
            sig, rank = es.signals(f)
        out[name] = {"bars": Bars.from_frame(name, f, extra=("atr", "ema20")), "signal": sig, "rank": rank,
                     "kind": kind, "gated": gated, "symbol": symbol, "start": f.index[0].date()}
    return out


def policy_for(assets: dict, regime_lookup):
    def make(name: str):
        a = assets[name]
        if a["kind"] == "metal":
            return es.SwingRSPolicy()
        return es.SwingRSPolicy(partial_fraction=0.4, regime_ok=regime_lookup if a["gated"] else None)
    return make


def run(assets: dict, names: list[str], start: date, end: date, cfg, regime_lookup, *, portfolio: bool):
    sc = cfg.get("etf_swing", {}) or {}
    chosen = {n: assets[n] for n in names if n in assets}
    if not chosen:
        return None
    return simulate(
        {n: a["bars"] for n, a in chosen.items()}, {n: a["signal"] for n, a in chosen.items()},
        {n: a["rank"] for n, a in chosen.items()}, policy_for(chosen, regime_lookup),
        strategy="etf_swing", start=start, end=end, mode=NEXT_OPEN, slippage=0.001,
        max_positions=int(sc.get("max_positions", 3)) if portfolio else None,
        initial_capital=float(sc.get("capital", 500000)) if portfolio else None,
        cfg=cfg, cost_fn=es.etf_costs(cfg),
        size_fn=es.risk_sizer(float(sc.get("risk_per_trade", 5000)), float(sc.get("max_position_value", 140000))),
    )


def judge_system(periods: dict) -> tuple[bool, list[str]]:
    reasons = []
    for name, s in periods.items():
        n = s.get("trades") or 0
        if n < PASS["min_trades"]:
            reasons.append(f"{name}: only {n} trades (need {PASS['min_trades']})")
        if n:
            if (s.get("profit_factor") or 0) < PASS["min_profit_factor"]:
                reasons.append(f"{name}: profit factor {num(s.get('profit_factor'), 2)} below {PASS['min_profit_factor']}")
            if (s.get("expectancy_pct") or 0) <= 0:
                reasons.append(f"{name}: average trade {num(s.get('expectancy_pct'), 2)}% after costs")
    return not reasons, reasons


def judge_portfolio(periods: dict) -> tuple[bool, list[str]]:
    reasons = []
    for name, p in periods.items():
        c, b = p["curve"], p["benchmark"]
        if c["cagr_pct"] <= b["cagr_pct"]:
            reasons.append(f"{name}: grew {num(c['cagr_pct'])}% a year, not above NIFTYBEES's {num(b['cagr_pct'])}%")
        if c["max_drawdown_pct"] < b["max_drawdown_pct"]:
            reasons.append(f"{name}: worst fall {num(c['max_drawdown_pct'])}%, deeper than NIFTYBEES's "
                           f"{num(b['max_drawdown_pct'])}%")
    return not reasons, reasons


def evaluate(label: str, assets: dict, nifty_close: pd.Series, benchmark: pd.DataFrame, cfg, today: date,
             periods=PERIODS) -> dict:
    regime_series = es.above_sma50(nifty_close)
    regime_series.index = [d.date() for d in regime_series.index]

    def regime_lookup(day) -> bool:
        value = regime_series.get(day)
        return bool(value) if value is not None else True

    metals = [n for n, a in assets.items() if a["kind"] == "metal"]
    sectors = [n for n, a in assets.items() if a["kind"] == "sector"]
    capital = float(cfg.get("etf_swing.capital", 500000))
    result = {"label": label, "assets": {n: {"symbol": a["symbol"], "from": a["start"].isoformat(),
                                             "signals": int(a["signal"].sum())} for n, a in assets.items()},
              "2A": {}, "2B": {}, "3": {}, "per_asset": {}, "rows": []}
    for name, start, end in periods:
        end = end or today
        for key, names in (("2A", metals), ("2B", sectors)):
            sim = run(assets, names, start, end, cfg, regime_lookup, portfolio=False)
            result[key][name] = research._brief(trade_stats(sim.trades)) if sim else {"trades": 0}
            if sim:
                result[key][name]["by_exit"] = trade_stats(sim.trades).get("by_exit", {})
        sim = run(assets, metals + sectors, start, end, cfg, regime_lookup, portfolio=True)
        curve = curve_stats(sim.equity, capital)
        curve["exposure"] = sim.exposure
        bench = curve_stats(research.benchmark_curve(benchmark, start, end, capital), capital)
        result["3"][name] = {"curve": curve, "benchmark": bench, "trades": research._brief(trade_stats(sim.trades))}
        result["rows"] += [dict(t.to_row(), period=name, dataset=label) for t in sim.trades]
    for n in assets:
        sim = run(assets, [n], periods[0][1], today, cfg, regime_lookup, portfolio=False)
        result["per_asset"][n] = research._brief(trade_stats(sim.trades)) if sim else {"trades": 0}
    return result


def main() -> int:
    started = time.time()
    cfg = load_config()
    today = date.today()
    REPORTS.mkdir(exist_ok=True)
    sc = cfg.get("etf_swing", {}) or {}

    # Breadth: the five largest members of each sector.
    constituents = {}
    for sector, symbols in (sc.get("breadth", {}) or {}).items():
        fetched = prices.get_price_history_batch([StockIdentity(s) for s in symbols], years=YEARS)
        constituents[sector] = {s: r.value for s, r in fetched.items() if r is not None and r.value is not None}
        log.info("Breadth %s: %d of %d members", sector, len(constituents[sector]), len(symbols))

    nifty_index = history("^NSEI")
    niftybees = history("NIFTYBEES.NS")
    if nifty_index is None or niftybees is None:
        log.error("No Nifty history")
        return 1

    long_assets = {n: (v[1], "metal", False) for n, v in (sc.get("metals") or {}).items()}
    long_assets |= {n: (v[1], "sector", bool(v[2])) for n, v in (sc.get("sectors") or {}).items()}
    real_assets = {n: (v[0], "metal", False) for n, v in (sc.get("metals") or {}).items()}
    real_assets |= {n: (v[0], "sector", bool(v[2])) for n, v in (sc.get("sectors") or {}).items()}

    long_built = build(long_assets, nifty_index["close"], constituents, use_volume=True)
    real_built = build(real_assets, niftybees["close"], constituents, use_volume=True)
    log.info("Long-history assets: %s", {n: a["start"].isoformat() for n, a in long_built.items()})
    log.info("Real ETFs: %s", {n: a["start"].isoformat() for n, a in real_built.items()})

    long_result = evaluate("long history", long_built, nifty_index["close"], niftybees, cfg, today)
    real_result = evaluate("real ETFs", real_built, niftybees["close"], niftybees, cfg, today,
                           periods=(("2022+", date(2022, 1, 1), None),))

    verdicts = {
        "2A": judge_system(long_result["2A"]),
        "2B": judge_system(long_result["2B"]),
        "3": judge_portfolio({k: v for k, v in long_result["3"].items()}),
    }
    for key, (ok, why) in verdicts.items():
        log.info("System %s: %s %s", key, "PASS" if ok else "FAIL", "; ".join(why))

    pd.DataFrame(long_result["rows"] + real_result["rows"]).to_csv(REPORTS / "etf_swing_trades.csv", index=False)
    write_json(long_result, real_result, verdicts, today)
    write_report(long_result, real_result, verdicts, today, time.time() - started)

    print("\n=== SUMMARY ===")
    for key, (ok, why) in verdicts.items():
        print(f"System {key}: {'PASS' if ok else 'FAIL'}  {'; '.join(why)}")
    for name, p in long_result["3"].items():
        print(f"System 3 {name}: {num(p['curve']['cagr_pct'])}%/{num(p['curve']['max_drawdown_pct'])}% vs "
              f"NIFTYBEES {num(p['benchmark']['cagr_pct'])}%/{num(p['benchmark']['max_drawdown_pct'])}%")
    return 0


def write_json(long_result, real_result, verdicts, today):
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

    strip = lambda r: {k: v for k, v in r.items() if k != "rows"}
    payload = {"generated": today.isoformat(), "criteria": PASS,
               "verdicts": {k: {"passed": ok, "reasons": why} for k, (ok, why) in verdicts.items()},
               "long_history": strip(long_result), "real_etfs": strip(real_result)}
    (REPORTS / "etf_swing_research.json").write_text(json.dumps(clean(payload), indent=2), encoding="utf-8")


def _system_rows(result, key):
    lines = ["| Period | Trades | Win rate | Avg win | Avg loss | Profit factor | Avg trade | Avg hold |",
             "|---|---|---|---|---|---|---|---|"]
    for name, s in result[key].items():
        if not s.get("trades"):
            lines.append(f"| {name} | 0 | - | - | - | - | - | - |")
            continue
        lines.append(f"| {name} | {s['trades']} | {pct(s.get('win_rate'))} | {num(s.get('avg_win_pct'))}% | "
                     f"{num(s.get('avg_loss_pct'))}% | {num(s.get('profit_factor'), 2)} | "
                     f"{num(s.get('expectancy_pct'), 2)}% | {num(s.get('avg_sessions'), 0)} sessions |")
    return lines


def _portfolio_rows(result):
    lines = ["| Period | Growth a year | Worst fall | NIFTYBEES growth | NIFTYBEES worst fall | Trades | Win rate | Invested |",
             "|---|---|---|---|---|---|---|---|"]
    for name, p in result["3"].items():
        c, b, t = p["curve"], p["benchmark"], p["trades"]
        lines.append(f"| {name} | {num(c['cagr_pct'])}% | {num(c['max_drawdown_pct'])}% | {num(b['cagr_pct'])}% | "
                     f"{num(b['max_drawdown_pct'])}% | {t.get('trades') or 0} | {pct(t.get('win_rate'))} | "
                     f"{pct(c.get('exposure'))} |")
    return lines


def write_report(long_result, real_result, verdicts, today, seconds):
    lines = [
        "# ETF swing systems: 2A metals, 2B sectors, 3 the portfolio",
        "",
        f"Generated {today.isoformat()} in {seconds / 60:.0f} minutes. Rules as proposed on 2026-10-17 (see "
        "src/strategy/etf_swing.py): trend and relative strength against the Nifty, a 3-7 day pullback to the "
        "20-day EMA, entry at the next open, a 1.5 x ATR stop, and at +2R a trail below the 20-day EMA (2B sells "
        "40% at +2R first). Portfolio: Rs 5 lakh, Rs 5,000 risked per position, at most Rs 1.4 lakh in one, at "
        "most 3, sector ETFs sold when the Nifty closes below its 50-day SMA. Groww delivery charges with ETF "
        "STT (0.001% on sells), plus 0.1% slippage each way.",
        "",
        "**Not replayed:** the iNAV premium check, the 10:15-14:30 order window and the earnings blackout. "
        "Breadth uses today's five largest members of each sector. Tax is not deducted.",
        "",
        "## Verdicts (long-history dataset, criteria fixed before running)",
        "",
    ]
    names = {"2A": "System 2A, metals", "2B": "System 2B, sectors", "3": "System 3, the portfolio"}
    for key, (ok, why) in verdicts.items():
        lines.append(f"- **{names[key]}: {'PASS' if ok else 'fail'}**" + (f". {'; '.join(why)}." if why else ""))
    for result in (long_result, real_result):
        lines += ["", f"## {result['label'].capitalize()}", "",
                  "Assets: " + ", ".join(f"{n} ({a['symbol']}, from {a['from']}, {a['signals']} signals)"
                                         for n, a in result["assets"].items()), "",
                  "### System 2A, metals: every signal as its own trade", ""] + _system_rows(result, "2A")
        lines += ["", "### System 2B, sectors: every signal as its own trade", ""] + _system_rows(result, "2B")
        lines += ["", "### System 3, the portfolio", ""] + _portfolio_rows(result)
        lines += ["", "### By asset, all years", "",
                  "| Asset | Trades | Win rate | Profit factor | Avg trade |", "|---|---|---|---|---|"]
        for n, s in result["per_asset"].items():
            lines.append(f"| {n} | {s.get('trades') or 0} | {pct(s.get('win_rate'))} | "
                         f"{num(s.get('profit_factor'), 2)} | {num(s.get('expectancy_pct'), 2)}% |")
    (REPORTS / "etf_swing_research.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
