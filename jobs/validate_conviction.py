"""Does conviction predict returns, or does it only feel like it should?

The screen score already failed this test - bucketing 114 backtest trades by
it gave the top bucket a 51% win rate against the bottom's 67%. Conviction is
the number that is *meant* to predict, and it drives position sizing, so it
should not be trusted until measured.

**What can honestly be measured, and what cannot.**

Only `price_frame` honours `as_of`. Fundamentals, shareholding, insider
disclosures and news are fetched as they stand today, so running the full
committee on a 2023 date would judge that dip using 2026 financials - it
would implicitly know the company survived. Delivery history in the database
covers a single session. So the equity, ownership and news desks cannot be
tested historically at all, and a conviction built partly from them would be
contaminated in a direction that flatters it.

What this measures instead is every price-derived signal that feeds
conviction, strictly point-in-time: each is computed from bars up to the
entry date and correlated with what happened afterwards. That answers the
useful half of the question - which of our inputs carry information - and
says plainly that the rest is unverified rather than pretending otherwise.

Returns are measured as excess over the Nifty 500. In a market that rose 20%,
a signal picking stocks that rose 20% has predicted nothing.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import indicators as ind
from src.config import load_config
from src.data import nse, prices
from src.learning import attribution

logging.basicConfig(level=logging.WARNING, format="%(message)s")
log = logging.getLogger("validate")

#: Signals available point-in-time from price data alone.
PRICE_SIGNALS = (
    "drawdown_pct",
    "rsi",
    "dma_slope_pct",
    "pct_vs_dma_long",
    "atr_pct",
    "screen_score",
    "screen_score_legacy",
    "dip_conviction",
)


def screen_score_at(metrics: dict, cfg) -> float:
    """The screener's own ranking score, recomputed point-in-time."""
    from src import screener

    return screener.score_candidate(metrics, cfg)



def legacy_screen_score(metrics: dict, cfg) -> float:
    """The scoring function as it stood before 2026-09-23, frozen here.

    Kept verbatim so the revision can be judged against it on symbols neither
    version was designed on. Delivery terms are omitted from both sides -
    historical delivery is not available - so this compares exactly the part
    that changed.
    """
    score = 0.0
    drawdown = metrics.get("drawdown_pct")
    if drawdown is not None:
        low = float(cfg.get("dip.drawdown_min_pct", 10.0))
        high = float(cfg.get("dip.drawdown_max_pct", 35.0))
        midpoint = (low + high) / 2.0
        score += 25.0 * max(0.0, 1.0 - abs(drawdown - midpoint) / (high - low))
    rsi = metrics.get("rsi")
    if rsi is not None:
        score += 20.0 * max(0.0, min(1.0, (float(cfg.get("dip.rsi_max", 40.0)) - rsi) / 15.0))
    slope = metrics.get("dma_slope_pct")
    if slope is not None:
        score += 15.0 * max(0.0, min(1.0, slope / 10.0))
    if metrics.get("near_support"):
        score += 5.0
    return round(score, 1)


def dip_conviction_at(metrics: dict, cfg) -> float:
    """The price-derived share of what the committee would call conviction.

    The dip and trend bots reduced to one number on the same 0-100 scale, so
    it can be compared against the screen score and against the desks that
    cannot be tested.
    """
    from src.agents.base import band_score
    from src.agents.schemas import score_to_conviction

    parts: list[float] = []

    drawdown = metrics.get("drawdown_pct")
    if drawdown is not None:
        low = float(cfg.get("dip.drawdown_min_pct", 10))
        high = float(cfg.get("dip.drawdown_max_pct", 35))
        midpoint = (low + high) / 2
        parts.append(5.0 * max(-1.0, 1.0 - abs(drawdown - midpoint) / (high - low) * 2))

    rsi = metrics.get("rsi")
    if rsi is not None:
        parts.append(band_score(-rsi, [(-25, 4.0), (-32, 2.5), (-40, 1.0), (-50, -1.0), (-1e9, -3.0)]))

    slope = metrics.get("dma_slope_pct")
    if slope is not None:
        parts.append(band_score(slope, [(15, 4.0), (8, 2.5), (2, 1.0), (0, 0.0), (-1e9, -4.0)]))

    distance = metrics.get("pct_vs_dma_long")
    if distance is not None:
        parts.append(band_score(distance, [(5, 1.5), (0, 2.5), (-5, 1.5), (-1e9, -2.0)]))

    return score_to_conviction(sum(parts) / len(parts)) if parts else 50.0


def observe(
    symbols: list[str],
    *,
    start: date,
    end: date,
    every: int,
    horizons: tuple[int, ...],
    cfg,
) -> list[dict]:
    """Walk history, recording each signal and what followed it."""
    from src.data.provider import StockIdentity

    stocks = [StockIdentity(s) for s in symbols]
    years = (date.today() - start).days / 365.25 + 1.5
    fetched = prices.get_price_history_batch(stocks, years=years)

    benchmark = None
    bench = prices.get_index_history("nifty500", years=years)
    if bench.usable and bench.value is not None:
        benchmark = bench.value

    observations: list[dict] = []
    longest = max(horizons)

    for stock in stocks:
        data = fetched.get(stock.symbol)
        if data is None or not data.usable or data.value is None or len(data.value) < 400:
            continue

        try:
            frame = ind.compute_indicator_frame(
                data.value,
                rsi_period=int(cfg.get("dip.rsi_period", 14)),
                atr_period=int(cfg.get("dip.atr_period", 14)),
                dma_long=int(cfg.get("dip.dma_long", 200)),
                dma_short=int(cfg.get("dip.dma_short", 50)),
                slope_lookback=int(cfg.get("dip.dma_slope_lookback_days", 126)),
            )
        except Exception:
            continue

        window = frame[(frame.index.date >= start) & (frame.index.date <= end)]
        dma_long = int(cfg.get("dip.dma_long", 200))

        for position in range(0, len(window), every):
            stamp = window.index[position]
            row = window.loc[stamp]

            # Only sample where the dip screen would have looked. Measuring
            # across every session answers a different question - whether
            # these signals work in general - not whether they rank the
            # candidates this system actually surfaces.
            drawdown = row.get("drawdown_pct")
            if pd.isna(drawdown) or not (
                float(cfg.get("dip.drawdown_min_pct", 10))
                <= drawdown
                <= float(cfg.get("dip.drawdown_max_pct", 35))
            ):
                continue

            long_dma = row.get(f"dma_{dma_long}")
            if pd.isna(long_dma) or pd.isna(row.get("rsi")) or pd.isna(row.get("dma_long_slope_pct")):
                continue

            metrics = {
                "drawdown_pct": float(drawdown),
                "rsi": float(row["rsi"]),
                "dma_slope_pct": float(row["dma_long_slope_pct"]),
                "pct_vs_dma_long": (float(row["close"]) - float(long_dma)) / float(long_dma) * 100.0,
                "atr_pct": None if pd.isna(row.get("atr_pct")) else float(row["atr_pct"]),
                "near_support": False,
            }
            metrics["screen_score"] = screen_score_at(metrics, cfg)
            metrics["screen_score_legacy"] = legacy_screen_score(metrics, cfg)
            metrics["dip_conviction"] = dip_conviction_at(metrics, cfg)

            record = {
                "symbol": stock.symbol,
                "date": stamp.date(),
                # Month buckets, so the cross-sectional IC has enough names to
                # rank against each other on any given slice.
                "period": (stamp.year, stamp.month),
                **metrics,
            }

            usable = False
            for horizon in horizons:
                value = attribution.forward_return(frame, stamp.date(), horizon, benchmark=benchmark)
                record[f"return_{horizon}"] = value
                # The market's own move over the same window, kept so the
                # results can be split by regime. A signal that only works
                # when everything is rising has not been shown to work.
                record[f"bench_{horizon}"] = (
                    attribution.forward_return(benchmark, stamp.date(), horizon)
                    if benchmark is not None else None
                )
                if value is not None:
                    usable = True

            if usable:
                observations.append(record)

    return observations


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure whether our signals predict")
    parser.add_argument("--universe", type=int, default=150, help="how many symbols")
    parser.add_argument("--offset", type=int, default=0,
                        help="skip this many symbols first - use for a holdout run")
    parser.add_argument("--years", type=float, default=6.0)
    parser.add_argument("--every", type=int, default=21, help="sample every N sessions")
    args = parser.parse_args()

    cfg = load_config()
    horizons = (21, 63, 126)

    universe = nse.get_universe(cfg.get("universe.index", "NIFTY 500"))
    if not universe.usable or not universe.value:
        print("Could not load the universe.")
        return 1
    symbols = [
        s.symbol for s in universe.value[args.offset : args.offset + args.universe]
    ]

    end = date.today() - timedelta(days=max(horizons) + 10)
    start = end - timedelta(days=int(args.years * 365.25))

    print(f"Sampling {len(symbols)} symbols, {start} to {end}, every {args.every} sessions.")
    print("Only bars where the dip screen would have looked.\n")

    observations = observe(
        symbols, start=start, end=end, every=args.every, horizons=horizons, cfg=cfg
    )
    print(f"{len(observations):,} point-in-time observations\n")

    if len(observations) < 50:
        print("Too few observations to say anything.")
        return 1

    results = attribution.study(observations, PRICE_SIGNALS, horizons)
    print(attribution.report(results))

    survivors = attribution.surviving_correction(results, alpha=0.10)
    print()
    print(f"  Twenty-one tests were run at once, so about two will look significant")
    print(f"  on pure noise. Correcting for that (Bonferroni, p <= {0.10/max(1,len(results)):.4f}),")
    if survivors:
        print(f"  {len(survivors)} survive:")
        for r in survivors:
            print(f"    {r.name} @ {r.horizon_days}d  IC {r.ic:+.3f}")
    else:
        print("  none survive.")

    # --- Within-period ranking: the question a screener actually asks.
    print("\n" + "=" * 96)
    print("CROSS-SECTIONAL: does the signal rank stocks against each other,")
    print("within the same month, facing the same market?")
    print("=" * 96)
    cs_results = []
    for horizon in horizons:
        for name in PRICE_SIGNALS:
            result = attribution.cross_sectional_ic(observations, name, horizon)
            if result:
                cs_results.append(result)
    for result in sorted(cs_results, key=lambda r: -abs(r.mean_ic)):
        print("  " + result.describe())

    # --- Old scoring against new, on whatever symbols this run used.
    print("")
    print("=" * 96)
    print("OLD SCORE vs NEW SCORE")
    print("=" * 96)
    print("  Delivery is excluded from both - no historical delivery data - so this")
    print("  isolates the price terms, which is the part that was revised.")
    for horizon in horizons:
        old_cs = attribution.cross_sectional_ic(observations, "screen_score_legacy", horizon)
        new_cs = attribution.cross_sectional_ic(observations, "screen_score", horizon)
        if not (old_cs and new_cs):
            continue
        print(f"\n  {horizon} sessions, within-month ranking:")
        print(f"    old  IC {old_cs.mean_ic:+.3f}  t {old_cs.t_stat:+5.2f}  "
              f"p {old_cs.p_value:.3f}  {old_cs.share_positive:4.0%} of months positive")
        print(f"    new  IC {new_cs.mean_ic:+.3f}  t {new_cs.t_stat:+5.2f}  "
              f"p {new_cs.p_value:.3f}  {new_cs.share_positive:4.0%} of months positive")

        for label, key in (("old", "screen_score_legacy"), ("new", "screen_score")):
            rows = [o for o in observations if o.get(f"return_{horizon}") is not None]
            frame = pd.DataFrame(rows)
            frame["b"] = pd.qcut(frame[key], 5, labels=False, duplicates="drop")
            grouped = frame.groupby("b")[f"return_{horizon}"].mean()
            print(f"    {label}  top-quintile minus bottom: "
                  f"{grouped.iloc[-1] - grouped.iloc[0]:+.2f}%")

    # --- Regime split: rising market versus falling.
    print("\n" + "=" * 96)
    print("REGIME SPLIT: is any edge just beta in disguise?")
    print("=" * 96)
    print("  High-volatility stocks outrun the index when it rises and fall further")
    print("  when it drops. Subtracting the index return does not remove that - so")
    print("  an ATR 'edge' measured over a rising decade may be beta, not skill.")
    for horizon in (63, 126):
        rising = [o for o in observations
                  if o.get(f"bench_{horizon}") is not None and o[f"bench_{horizon}"] > 0
                  and o.get(f"return_{horizon}") is not None]
        falling = [o for o in observations
                   if o.get(f"bench_{horizon}") is not None and o[f"bench_{horizon}"] <= 0
                   and o.get(f"return_{horizon}") is not None]
        print(f"\n  {horizon} sessions  ({len(rising):,} obs in rising markets, "
              f"{len(falling):,} in falling)")
        for label, subset in (("market rose", rising), ("market fell", falling)):
            if len(subset) < 100:
                print(f"    {label:<14} too few observations")
                continue
            for name in ("screen_score", "screen_score_legacy", "atr_pct", "pct_vs_dma_long"):
                res = attribution.information_coefficient(
                    [o.get(name) for o in subset],
                    [o.get(f"return_{horizon}") for o in subset],
                    name=name, horizon_days=horizon,
                )
                if res:
                    print(f"    {label:<14} {name:<16} IC {res.ic:+.3f}  "
                          f"p {res.p_value:.3f}  n {res.n:,}")

    # The headline: does the price-derived conviction rank outcomes?
    print("\n" + "=" * 96)
    print("CONVICTION, price-derived component only")
    print("=" * 96)
    for horizon in horizons:
        rows = [o for o in observations if o.get(f"return_{horizon}") is not None]
        if len(rows) < 50:
            continue
        frame = pd.DataFrame(rows)
        frame["bucket"] = pd.qcut(frame["dip_conviction"], 5, labels=False, duplicates="drop")
        grouped = frame.groupby("bucket")[f"return_{horizon}"].agg(["mean", "count"])
        print(f"\n  {horizon} sessions, excess over Nifty 500:")
        for bucket, row in grouped.iterrows():
            label = ["lowest", "low", "mid", "high", "highest"][int(bucket)] if bucket < 5 else str(bucket)
            print(f"    {label:<8} conviction  {row['mean']:+6.2f}%   (n={int(row['count'])})")
        top = grouped["mean"].iloc[-1]
        bottom = grouped["mean"].iloc[0]
        print(f"    {'spread':<8}             {top - bottom:+6.2f}%")

    print("\n" + "=" * 96)
    print("NOT MEASURED HERE")
    print("=" * 96)
    print("  The equity, ownership and news desks - which together carry 75% of the")
    print("  committee's desk weight - cannot be tested historically. Fundamentals,")
    print("  shareholding, insider filings and news are only available as they stand")
    print("  today, and delivery history in the database covers one session. Any")
    print("  conviction computed from them on a past date would know how the story")
    print("  ended.")
    print()
    print("  Measuring those needs the live ledger: conviction is recorded with every")
    print("  committee run now, so forward returns can be checked against it at the")
    print("  30/90/180-day marks as real time passes. That is the only honest route.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
