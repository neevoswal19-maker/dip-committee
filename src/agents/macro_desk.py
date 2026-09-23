"""Head of Industry & Geopolitical Research - two analysts.

Both answer the same underlying question from different directions: is the
weakness in this stock about the company, or about everything around it? A
dip driven by a sector-wide derating is a different proposition from one where
a single company has fallen while its peers held.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from src.agents import lexicon
from src.agents.base import Analyst, band_score
from src.agents.schemas import Evidence, Verdict

DESK = "macro"


def _return_over(series: pd.Series, sessions: int) -> float | None:
    series = pd.to_numeric(series, errors="coerce").dropna()
    if len(series) < sessions + 1:
        return None
    start, end = float(series.iloc[-sessions - 1]), float(series.iloc[-1])
    if start <= 0:
        return None
    return (end - start) / start * 100.0


class IndustryResearchAnalyst(Analyst):
    """Has the stock fallen with its sector, or on its own."""

    bot_id = "industry_research_analyst"
    desk = DESK
    name = "Industry Research Analyst"
    requires = ("price_frame",)

    def gather(self, ctx: Any) -> dict[str, Any]:
        frame = ctx.price_frame
        if frame is None or frame.empty:
            return {"data_available": False, "note": "no price history"}

        sector_frame = ctx.get("sector_index")
        benchmark_frame = ctx.get("benchmark_index")

        metrics: dict[str, Any] = {"sector": ctx.sector}

        for label, source in (("stock", frame), ("sector", sector_frame), ("market", benchmark_frame)):
            if isinstance(source, pd.DataFrame) and not source.empty and "close" in source.columns:
                for window, sessions in (("1m", 21), ("3m", 63), ("6m", 126)):
                    metrics[f"{label}_{window}"] = _return_over(source["close"], sessions)

        if metrics.get("sector_3m") is None and metrics.get("market_3m") is None:
            return {
                "data_available": False,
                "note": f"no sector or benchmark index available for {ctx.sector or 'this stock'}",
            }

        return metrics

    def judge(self, metrics: dict[str, Any]) -> Verdict:
        findings: list[str] = []
        components: list[float] = []

        sector_name = metrics.get("sector") or "its sector"
        stock_3m = metrics.get("stock_3m")
        sector_3m = metrics.get("sector_3m")
        market_3m = metrics.get("market_3m")

        reference = sector_3m if sector_3m is not None else market_3m
        reference_name = sector_name if sector_3m is not None else "the Nifty 500"

        if stock_3m is not None and reference is not None:
            relative = stock_3m - reference
            findings.append(
                f"Over three months the stock is {stock_3m:+.1f}% against "
                f"{reference:+.1f}% for {reference_name}, a relative {relative:+.1f}%."
            )

            # A stock that has fallen much harder than its sector is either a
            # genuine bargain or the one with the problem. Mild weakness is
            # the sweet spot; extreme divergence is a warning.
            if relative < -25:
                components.append(-2.0)
                findings.append(
                    "It has fallen far harder than its peers, which usually means the "
                    "market sees something company-specific rather than sectoral."
                )
            elif relative < -8:
                components.append(1.5)
                findings.append(
                    "Modest underperformance against a steadier sector - the pattern a "
                    "recoverable dip tends to have."
                )
            elif relative > 10:
                components.append(-0.5)
                findings.append("It has outperformed its sector, so this dip is shallower than it looks.")
            else:
                components.append(0.5)

        if sector_3m is not None and market_3m is not None:
            sector_relative = sector_3m - market_3m
            findings.append(
                f"{sector_name} is {sector_relative:+.1f}% against the broad market over three months."
            )
            components.append(band_score(sector_relative, [(10, 2.0), (0, 1.0), (-10, -0.5), (-1e9, -2.0)]))

        stock_6m = metrics.get("stock_6m")
        if stock_6m is not None and stock_3m is not None and stock_3m > stock_6m:
            findings.append("Three-month performance is better than six-month - the decline may be easing.")
            components.append(1.0)

        score = sum(components) / len(components) if components else 0.0
        confidence = 0.55 if sector_3m is not None else 0.3

        return self.verdict(
            score, confidence, findings,
            [Evidence("stock_3m_pct", None if stock_3m is None else round(stock_3m, 1)),
             Evidence("sector_3m_pct", None if sector_3m is None else round(sector_3m, 1)),
             Evidence("market_3m_pct", None if market_3m is None else round(market_3m, 1))],
        )


class GeopoliticalRiskAnalyst(Analyst):
    """Macro conditions, weighted by what this sector is actually exposed to.

    A crude spike is good news for an oil producer and bad for a paint maker.
    Scoring the macro variables without that mapping would be noise, so each
    z-score is multiplied by the sector's known sensitivity to it.
    """

    bot_id = "geopolitical_risk_analyst"
    desk = DESK
    name = "Geopolitical Risk Analyst"
    requires = ("macro",)

    def gather(self, ctx: Any) -> dict[str, Any]:
        series = ctx.get("macro")
        if not series:
            return {"data_available": False, "note": "no macro series available"}

        readings: dict[str, dict[str, float]] = {}
        for name, frame in series.items():
            if not isinstance(frame, pd.DataFrame) or frame.empty or "close" not in frame:
                continue
            closes = pd.to_numeric(frame["close"], errors="coerce").dropna()
            if len(closes) < 60:
                continue

            latest = float(closes.iloc[-1])
            mean = float(closes.tail(252).mean())
            std = float(closes.tail(252).std())

            readings[name] = {
                "latest": latest,
                "z_score": (latest - mean) / std if std > 0 else 0.0,
                "change_3m_pct": _return_over(closes, 63) or 0.0,
            }

        if not readings:
            return {"data_available": False, "note": "macro series too short to score"}

        sector = ctx.sector or ""
        sensitivity = lexicon.SECTOR_MACRO_SENSITIVITY.get(sector, {})

        return {
            "sector": sector,
            "readings": readings,
            "sensitivity": sensitivity,
            "has_sector_map": bool(sensitivity),
        }

    def judge(self, metrics: dict[str, Any]) -> Verdict:
        readings = metrics["readings"]
        sensitivity = metrics["sensitivity"]
        sector = metrics["sector"] or "this sector"

        findings: list[str] = []
        exposure = 0.0

        labels = {
            "crude_brent": "Brent crude", "usdinr": "the rupee against the dollar",
            "india_vix": "India VIX", "us_10y": "the US 10-year yield",
            "gold": "gold", "nifty50": "the Nifty 50",
        }

        for name, reading in readings.items():
            if name in ("nifty50", "nifty500"):
                continue
            z = reading["z_score"]
            if abs(z) < 1.0:
                continue
            direction = "well above" if z > 0 else "well below"
            findings.append(
                f"{labels.get(name, name)} is {direction} its one-year average "
                f"({z:+.1f} standard deviations, {reading['change_3m_pct']:+.1f}% over three months)."
            )
            weight = sensitivity.get(name, 0.0)
            if weight:
                exposure += z * weight

        vix = readings.get("india_vix")
        if vix and vix["z_score"] > 1.5:
            findings.append(
                "Volatility is elevated. That is not itself a reason to avoid a dip - "
                "it is usually why the dip exists - but position sizing should reflect it."
            )
            exposure -= 0.3

        if not metrics["has_sector_map"]:
            findings.append(
                f"No macro sensitivity mapping for {sector}, so these readings are reported "
                f"as context rather than scored against this company."
            )
        elif exposure:
            direction = "favourable" if exposure > 0 else "unfavourable"
            findings.append(
                f"Weighted for {sector}'s known exposures, current macro conditions are "
                f"{direction} ({exposure:+.2f})."
            )

        if not findings:
            findings.append("No macro variable is more than one standard deviation from its average.")

        score = max(-3.0, min(3.0, exposure * 1.5))
        confidence = 0.45 if metrics["has_sector_map"] else 0.2

        return self.verdict(
            score, confidence, findings,
            [Evidence(f"{k}_z", round(v["z_score"], 2)) for k, v in list(readings.items())[:5]],
        )


ANALYSTS = (IndustryResearchAnalyst, GeopoliticalRiskAnalyst)
