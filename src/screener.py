"""The three-stage screen that narrows the Nifty 500 to a handful of names.

Stage 1  Quality gate      Would I want to own this at any price?
Stage 2  Dip detection     Is this a dip, or the early part of a collapse?
Stage 3  Delivery          Is anyone actually accumulating into the fall?

The stages run cheapest-first and in that order for a reason: Stage 2 needs
only price data, which is fast and free, so it disqualifies most of the
universe before Stage 1's per-stock fundamentals request is ever made.

Nothing here calls an LLM. This layer is deterministic, backtestable and
costs nothing to run, which is what lets it scan 500 stocks every evening
while the committee only ever looks at the handful that survive.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from src import indicators as ind
from src.config import load_config
from src.data import fundamentals as fnd
from src.data import nse, prices
from src.data.provider import DataResult, StockIdentity

log = logging.getLogger(__name__)


@dataclass
class StageResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)   # why it failed
    notes: list[str] = field(default_factory=list)     # what was not checkable


@dataclass
class ScreenResult:
    """One stock's full journey through the screen."""

    symbol: str
    name: str | None = None
    sector: str | None = None

    quality: StageResult | None = None
    dip: StageResult | None = None
    delivery: StageResult | None = None

    metrics: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0
    rank: int | None = None
    error: str | None = None

    @property
    def passed_all(self) -> bool:
        return all(
            stage is not None and stage.passed
            for stage in (self.quality, self.dip, self.delivery)
        )

    @property
    def failure_summary(self) -> str:
        for label, stage in (("quality", self.quality), ("dip", self.dip), ("delivery", self.delivery)):
            if stage is not None and not stage.passed:
                return f"{label}: {'; '.join(stage.reasons)}"
        return "passed" if self.passed_all else "not evaluated"


# --- Stage 2: dip detection -------------------------------------------------


def evaluate_dip(frame: pd.DataFrame, cfg: Any) -> tuple[StageResult, dict[str, Any]]:
    """Is the decline a dip inside an uptrend, or a falling knife?

    The 200-DMA condition is the one that matters. Price may sit below the
    long average provided the average itself is still rising - that is a
    pullback. Price below a falling average is a downtrend, and buying it is
    the single most expensive mistake this strategy can make.
    """
    reasons: list[str] = []
    notes: list[str] = []
    metrics: dict[str, Any] = {}

    dma_long = int(cfg.get("dip.dma_long", 200))
    dma_short = int(cfg.get("dip.dma_short", 50))

    if len(frame) < dma_long + 20:
        return StageResult(False, [f"only {len(frame)} sessions of history, need {dma_long + 20}"]), metrics

    last = frame.iloc[-1]
    close = float(last["close"])

    metrics.update(
        {
            "close": round(close, 2),
            "rsi": None if pd.isna(last.get("rsi")) else round(float(last["rsi"]), 1),
            "atr": None if pd.isna(last.get("atr")) else round(float(last["atr"]), 2),
            "atr_pct": None if pd.isna(last.get("atr_pct")) else round(float(last["atr_pct"]), 2),
            "drawdown_pct": None if pd.isna(last.get("drawdown_pct")) else round(float(last["drawdown_pct"]), 1),
            "high_52w": None if pd.isna(last.get("high_52w")) else round(float(last["high_52w"]), 2),
            "low_52w": None if pd.isna(last.get("low_52w")) else round(float(last["low_52w"]), 2),
            f"dma_{dma_long}": None if pd.isna(last.get(f"dma_{dma_long}")) else round(float(last[f"dma_{dma_long}"]), 2),
            f"dma_{dma_short}": None if pd.isna(last.get(f"dma_{dma_short}")) else round(float(last[f"dma_{dma_short}"]), 2),
            "dma_slope_pct": None if pd.isna(last.get("dma_long_slope_pct")) else round(float(last["dma_long_slope_pct"]), 2),
        }
    )

    # --- Drawdown band
    drawdown = metrics["drawdown_pct"]
    low = float(cfg.get("dip.drawdown_min_pct", 10.0))
    high = float(cfg.get("dip.drawdown_max_pct", 35.0))
    if drawdown is None:
        reasons.append("drawdown could not be computed")
    elif drawdown < low:
        reasons.append(f"only {drawdown:.1f}% off the 52-week high, below the {low:.0f}% threshold")
    elif drawdown > high:
        reasons.append(f"{drawdown:.1f}% off the high exceeds the {high:.0f}% ceiling - likely a broken thesis, not a dip")

    # --- Trend filter
    long_dma = metrics[f"dma_{dma_long}"]
    slope = metrics["dma_slope_pct"]
    tolerance = float(cfg.get("dip.dma_tolerance_below_pct", 5.0))
    min_slope = float(cfg.get("dip.dma_slope_min_pct", 0.0))

    # The slope condition applies wherever the price sits, not only when it
    # is below the average. A stock that has crashed and bounced back above
    # its own steeply falling 200 DMA looks fine on a price-vs-average test
    # and is precisely the falling knife this strategy exists to avoid.
    if slope is None:
        notes.append(f"{dma_long} DMA slope unavailable, trend direction unverified")
    elif slope < min_slope:
        reasons.append(
            f"the {dma_long} DMA is falling ({slope:.1f}% over 6 months) - "
            f"the long-term trend is down, whatever the price is doing against it"
        )

    if long_dma is None:
        reasons.append(f"{dma_long} DMA unavailable")
    else:
        distance = (close - long_dma) / long_dma * 100.0
        metrics["pct_vs_dma_long"] = round(distance, 2)

        if distance < -tolerance:
            reasons.append(
                f"{abs(distance):.1f}% below the {dma_long} DMA, beyond the {tolerance:.0f}% tolerance"
            )

    # --- Momentum
    rsi = metrics["rsi"]
    rsi_max = float(cfg.get("dip.rsi_max", 40.0))
    if rsi is None:
        reasons.append("RSI unavailable")
    elif rsi > rsi_max:
        reasons.append(f"RSI {rsi:.1f} above {rsi_max:.0f} - not yet oversold")

    # --- Not a new low
    if cfg.get("dip.reject_new_52w_low", True):
        low_52w = metrics["low_52w"]
        if low_52w is not None and close <= low_52w * 1.005:
            reasons.append("at or near a new 52-week low - no evidence the fall has stopped")

    # --- Near support
    proximity = float(cfg.get("dip.support_proximity_pct", 5.0))
    support = ind.nearest_support(
        close, frame["low"], lookback=int(cfg.get("dip.swing_low_lookback_days", 252))
    )
    near_support = False

    if support is not None:
        gap = (close - support) / close * 100.0
        metrics["support_level"] = round(support, 2)
        metrics["support_distance_pct"] = round(gap, 2)
        near_support = 0 <= gap <= proximity

    if long_dma is not None and abs((close - long_dma) / close * 100.0) <= proximity:
        near_support = True
        metrics["at_dma_support"] = True

    metrics["near_support"] = near_support
    if not near_support:
        notes.append(f"not within {proximity:.0f}% of a support level")

    return StageResult(not reasons, reasons, notes), metrics


# --- Stage 3: delivery confirmation -----------------------------------------


def evaluate_delivery(
    delivery_frame: pd.DataFrame | None,
    cfg: Any,
) -> tuple[StageResult, dict[str, Any]]:
    """Is settled ownership rising while the price falls?

    Delivery percentage is the share of traded volume that actually moved
    into demat accounts rather than being squared off intraday. Elevated
    delivery on down days means someone is taking stock off the sellers'
    hands and keeping it.

    The persistence requirement is the important half. A single block deal
    spikes one session's delivery and looks identical to quiet accumulation,
    so the elevation has to repeat before it counts.
    """
    reasons: list[str] = []
    notes: list[str] = []
    metrics: dict[str, Any] = {}

    if delivery_frame is None or delivery_frame.empty:
        return StageResult(False, ["no delivery data available"]), metrics

    window = int(cfg.get("delivery.avg_window_days", 20))
    if len(delivery_frame) < window:
        return StageResult(False, [f"only {len(delivery_frame)} sessions of delivery data, need {window}"]), metrics

    df = delivery_frame.sort_index()
    delivery_pct = pd.to_numeric(df["delivery_pct"], errors="coerce")
    close = pd.to_numeric(df["close"], errors="coerce")

    avg_delivery = float(delivery_pct.tail(window).mean())
    latest_delivery = float(delivery_pct.iloc[-1]) if pd.notna(delivery_pct.iloc[-1]) else None

    metrics["delivery_pct_latest"] = None if latest_delivery is None else round(latest_delivery, 1)
    metrics["delivery_pct_avg"] = round(avg_delivery, 1)

    # --- Absolute floor
    minimum = float(cfg.get("delivery.min_delivery_pct", 40.0))
    if avg_delivery < minimum:
        reasons.append(
            f"average delivery of {avg_delivery:.1f}% is below {minimum:.0f}% - "
            f"this is a traded stock, not an owned one"
        )

    # --- Elevation on down days
    ratio_min = float(cfg.get("delivery.down_day_delivery_ratio_min", 1.10))
    ratios = ind.down_day_delivery_ratio(close, delivery_pct, window).dropna()

    if ratios.empty:
        reasons.append("no down days in the window to measure delivery against")
    else:
        recent_ratio = float(ratios.iloc[-1])
        mean_ratio = float(ratios.tail(10).mean())
        metrics["down_day_delivery_ratio"] = round(recent_ratio, 2)
        metrics["down_day_delivery_ratio_avg10"] = round(mean_ratio, 2)

        if mean_ratio < ratio_min:
            reasons.append(
                f"delivery on down days is running at {mean_ratio:.2f}x its average, "
                f"below the {ratio_min:.2f}x threshold - selling is not being absorbed"
            )

    # --- Persistence
    required = int(cfg.get("delivery.persistence_sessions_required", 3))
    lookback = int(cfg.get("delivery.persistence_lookback", 10))
    persistence = ind.delivery_persistence(close, delivery_pct, window, lookback, ratio_min)
    metrics["delivery_persistence"] = persistence

    if persistence < required:
        reasons.append(
            f"elevated delivery on only {persistence} of the last {lookback} down sessions "
            f"(need {required}) - consistent with a one-off block deal rather than accumulation"
        )

    # --- Liquidity
    if "turnover_lacs" in df.columns:
        turnover_cr = pd.to_numeric(df["turnover_lacs"], errors="coerce").tail(window).mean() / 100.0
        metrics["avg_turnover_cr"] = round(float(turnover_cr), 1)
        floor = float(cfg.get("delivery.min_avg_traded_value_cr", 5.0))
        if turnover_cr < floor:
            reasons.append(f"average turnover of Rs {turnover_cr:.1f} cr/day is below the Rs {floor:.0f} cr floor")

    return StageResult(not reasons, reasons, notes), metrics


# --- Scoring ----------------------------------------------------------------


def score_candidate(metrics: dict[str, Any], cfg: Any) -> float:
    """Rank surviving candidates 0-100.

    This orders the shortlist for the committee's attention; it is not a
    verdict. Everything scored here already passed all three stages, so the
    question is no longer "is this valid" but "which of these valid ones
    should the expensive analysis look at first".

    **Revised 2026-09-23, after measurement.** The previous weights were
    reasonable-sounding and did not work: across 4,321 point-in-time
    observations the old score had an information coefficient of -0.010 at 21
    sessions, -0.013 at 63 and -0.022 at 126 - zero, tipping slightly the
    wrong way. `jobs/validate_conviction.py` reproduces that.

    Three things changed, each because the data said so:

    * **Dip depth no longer peaks in the middle of the band.** The old hump
      rewarded a ~22% drawdown most. Measured within-month, deeper drawdowns
      did *worse* (cross-sectional IC -0.032 at 126 sessions, positive in only
      46% of months). Depth is now scored monotonically - shallower is better
      inside the band - and its weight is cut from 25 to 15.
    * **Distance above the 200-DMA is added, at the largest price weight.** It
      was absent entirely, and it is the one price signal that survived every
      correction: within-month IC +0.085 at 126 sessions, t +4.38, positive in
      71% of the 70 months, and it does not flip sign when the market falls.
      Among candidates the screen surfaces, the shallower dips win.
    * **RSI drops from 20 to 5.** Its IC never cleared +0.019 at any horizon
      and never approached significance. It is kept at token weight because it
      is the conventional oversold read and its absence would be surprising,
      not because it was shown to work.

    Deliberately *not* changed:

    * **Delivery keeps its 35 points.** It could not be measured - the
      database holds a single session of delivery history, so there was
      nothing to backtest against. Cutting an unmeasured signal is not the
      same as cutting a disproven one, and delivery is the India-specific
      edge this whole strategy rests on. It stays until there is evidence,
      which will take months of accumulated `price_bars` to gather.
    * **ATR is not added**, despite reading as the strongest signal in the
      pooled test (+0.081 at 126 sessions, the only survivor of a Bonferroni
      correction). The regime split disqualified it: +0.108 when the market
      rose, -0.053 when it fell. A signal whose sign follows the market is
      measuring beta. Buying high-ATR names would have looked brilliant over
      this particular six years and would raise drawdowns without adding edge.
    """
    score = 0.0

    # Dip depth, monotone: within the band, shallower beats deeper.
    drawdown = metrics.get("drawdown_pct")
    if drawdown is not None:
        low = float(cfg.get("dip.drawdown_min_pct", 10.0))
        high = float(cfg.get("dip.drawdown_max_pct", 35.0))
        shallowness = (high - float(drawdown)) / (high - low)
        score += 15.0 * max(0.0, min(1.0, shallowness))

    # Distance above the long DMA - the strongest measured price signal.
    # Scored over a -10% to +10% span: at or above the average earns full
    # marks, well below it earns nothing. A stock can be far off its 52-week
    # high and still sit above a rising 200-DMA, which is precisely the
    # distinction the old score was blind to.
    distance = metrics.get("pct_vs_dma_long")
    if distance is not None:
        score += 30.0 * max(0.0, min(1.0, (float(distance) + 10.0) / 20.0))

    # Oversold. Token weight: measured at no better than noise.
    rsi = metrics.get("rsi")
    if rsi is not None:
        score += 5.0 * max(0.0, min(1.0, (float(cfg.get("dip.rsi_max", 40.0)) - rsi) / 15.0))

    # Delivery conviction: the India-specific edge, and still unmeasured.
    ratio = metrics.get("down_day_delivery_ratio_avg10")
    if ratio is not None:
        score += 25.0 * max(0.0, min(1.0, (ratio - 1.0) / 0.5))

    persistence = metrics.get("delivery_persistence")
    if persistence is not None:
        score += 10.0 * min(1.0, persistence / 5.0)

    # Trend strength. Halved: the 200-DMA slope is already an unconditional
    # pass/fail gate in `evaluate_dip`, so scoring it again partly rewards a
    # test the candidate had to clear to get here at all.
    slope = metrics.get("dma_slope_pct")
    if slope is not None:
        score += 10.0 * max(0.0, min(1.0, float(slope) / 10.0))

    if metrics.get("near_support"):
        score += 5.0

    return round(min(100.0, score), 1)


# --- Orchestration ----------------------------------------------------------


def screen_stock(
    stock: StockIdentity,
    cfg: Any,
    *,
    check_quality: bool = True,
) -> ScreenResult:
    """Run one stock through all three stages.

    Stage 2 runs first because price data is cheap and rejects most of the
    universe; fundamentals are only fetched for what survives it.
    """
    result = ScreenResult(symbol=stock.symbol, name=stock.name, sector=stock.sector)

    try:
        history = prices.get_price_history(stock, years=float(cfg.get("data.history_years", 6)))
        if not history.usable or history.value is None or history.value.empty:
            result.error = f"no price history ({history.note or history.status.value})"
            result.dip = StageResult(False, [result.error])
            return result

        frame = ind.compute_indicator_frame(
            history.value,
            rsi_period=int(cfg.get("dip.rsi_period", 14)),
            atr_period=int(cfg.get("dip.atr_period", 14)),
            dma_long=int(cfg.get("dip.dma_long", 200)),
            dma_short=int(cfg.get("dip.dma_short", 50)),
            slope_lookback=int(cfg.get("dip.dma_slope_lookback_days", 126)),
        )

        close = float(frame["close"].iloc[-1])
        min_price = float(cfg.get("quality_gate.min_price", 50.0))
        if close < min_price:
            result.dip = StageResult(False, [f"price Rs {close:,.2f} below the Rs {min_price:,.0f} floor"])
            result.metrics["close"] = round(close, 2)
            return result

        result.dip, dip_metrics = evaluate_dip(frame, cfg)
        result.metrics.update(dip_metrics)

        if not result.dip.passed:
            return result

        # --- Stage 1, only for names that survived the dip screen
        if check_quality:
            metrics = fnd.get_fundamentals(stock)
            if metrics.usable and metrics.value:
                result.metrics.update(
                    {k: v for k, v in metrics.value.items() if k not in ("symbol", "forensics")}
                )
                result.metrics["forensics"] = metrics.value.get("forensics", {})
                passed, reasons = fnd.passes_quality_gate(metrics.value, cfg)
                result.quality = StageResult(passed, reasons)
                if not result.sector:
                    result.sector = metrics.value.get("sector")
            else:
                result.quality = StageResult(False, [f"fundamentals unavailable ({metrics.note or 'unknown'})"])

            if not result.quality.passed:
                return result
        else:
            result.quality = StageResult(True, [], ["quality gate skipped"])

        # --- Stage 3
        delivery = nse.get_delivery_history(stock, days=int(cfg.get("delivery.avg_window_days", 20)) * 3)
        result.delivery, delivery_metrics = evaluate_delivery(delivery.value, cfg)
        result.metrics.update(delivery_metrics)

        if result.passed_all:
            result.score = score_candidate(result.metrics, cfg)

    except Exception as exc:  # one bad symbol must not stop a 500-stock scan
        log.exception("Screening failed for %s", stock.symbol)
        result.error = f"{type(exc).__name__}: {exc}"

    return result


def screen_universe(
    index: str | None = None,
    cfg: Any = None,
    *,
    limit: int | None = None,
    check_quality: bool = True,
    progress: Any = None,
) -> list[ScreenResult]:
    """Screen an entire index, ranked best-first.

    Returns every result, not just the survivors, so the dashboard can show
    how many fell at each stage. A screen that rejects 500 of 500 is either
    a market with no opportunities or a broken data feed, and those look
    identical unless the failures are kept.
    """
    cfg = cfg or load_config()
    index = index or cfg.get("universe.index", "NIFTY 500")

    universe = nse.get_universe(index)
    if not universe.usable or not universe.value:
        log.error("Could not load universe %s: %s", index, universe.note)
        return []

    stocks = universe.value
    excluded = set(cfg.get("universe.exclude_symbols", []) or [])
    stocks = [s for s in stocks if s.symbol not in excluded]
    if limit:
        stocks = stocks[:limit]

    # Warm the price cache in bulk before screening. Fetching one symbol at a
    # time costs ~4.4s each, almost all of it round-trip latency; batched it
    # is under a tenth of a second. screen_stock then reads from cache.
    try:
        prices.get_price_history_batch(stocks, years=float(cfg.get("data.history_years", 6)))
    except Exception as exc:
        log.warning("Batch price prefetch failed, falling back to per-symbol: %s", exc)

    results: list[ScreenResult] = []
    for i, stock in enumerate(stocks, start=1):
        if progress is not None:
            progress(i, len(stocks), stock.symbol)
        results.append(screen_stock(stock, cfg, check_quality=check_quality))

    survivors = sorted(
        [r for r in results if r.passed_all], key=lambda r: r.score, reverse=True
    )
    for rank, result in enumerate(survivors, start=1):
        result.rank = rank

    return results


def summarise(results: list[ScreenResult]) -> dict[str, Any]:
    """Funnel counts, so a broken feed is distinguishable from a quiet market."""
    total = len(results)
    errored = sum(1 for r in results if r.error)
    dip_pass = sum(1 for r in results if r.dip and r.dip.passed)
    quality_pass = sum(1 for r in results if r.quality and r.quality.passed)
    delivery_pass = sum(1 for r in results if r.delivery and r.delivery.passed)
    survivors = [r for r in results if r.passed_all]

    return {
        "universe_size": total,
        "errored": errored,
        "passed_dip": dip_pass,
        "passed_quality": quality_pass,
        "passed_delivery": delivery_pass,
        "candidates": len(survivors),
        "top": [
            {"symbol": r.symbol, "score": r.score, "sector": r.sector, "close": r.metrics.get("close")}
            for r in sorted(survivors, key=lambda r: r.score, reverse=True)[:10]
        ],
    }
