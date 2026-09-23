"""Head of Market Activity & Ownership Research - four analysts.

The desk that carries the strategy's distinctive edge. Everything here is
arithmetic over data that most retail screeners never look at: who is actually
taking delivery of the shares, who is selling, and whether the people with the
best information are buying.

This is also the desk most exposed to NSE going quiet. Each bot reports
`data_available: false` rather than inferring, because "no insider selling
disclosed" and "we could not reach the disclosure feed" are opposite readings.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from src import screener
from src.agents.base import Analyst, band_score
from src.agents.schemas import Evidence, Verdict

DESK = "ownership"

#: Shareholding-pattern category names vary between filings, so each holder
#: type is matched against several spellings.
HOLDER_PATTERNS = {
    "promoter": ("promoter", "promoters"),
    "fii": ("foreign portfolio", "foreign institutional", "fii", "fpi"),
    "dii": ("domestic institutional", "dii", "insurance", "banks"),
    "mutual_fund": ("mutual fund", "mf"),
    "public": ("public", "retail", "individual"),
}


class MarketVolumeIntelligenceAnalyst(Analyst):
    """Is settled ownership rising while the price falls.

    Reuses `screener.evaluate_delivery` so the committee measures exactly what
    the screen measured - if these ever diverged, a stock could pass the screen
    and be marked down by the committee for the same data.
    """

    bot_id = "market_volume_intelligence_analyst"
    desk = DESK
    name = "Market Volume Intelligence Analyst"
    requires = ("delivery",)

    def gather(self, ctx: Any) -> dict[str, Any]:
        frame = ctx.get("delivery")
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            return {"data_available": False, "note": ctx.note_for("delivery")}

        stage, metrics = screener.evaluate_delivery(frame, self.cfg)
        return {
            **metrics,
            "sessions": len(frame),
            "stage_passed": stage.passed,
            "stage_reasons": stage.reasons,
        }

    def judge(self, metrics: dict[str, Any]) -> Verdict:
        findings: list[str] = []
        components: list[float] = []
        red_flags: list[str] = []

        ratio = metrics.get("down_day_delivery_ratio_avg10")
        threshold = float(self.cfg.get("delivery.down_day_delivery_ratio_min", 1.05))

        if ratio is not None:
            findings.append(
                f"On down days, delivery is running at {ratio:.2f}x its 20-day average "
                f"(threshold {threshold:.2f}x)."
            )
            components.append(band_score(ratio, [(1.25, 4.5), (1.15, 3.5), (1.08, 2.0),
                                                 (1.02, 0.5), (0.95, -1.5), (-1e9, -3.0)]))
            if ratio >= 1.15:
                findings.append(
                    "Someone is taking stock off the sellers and keeping it - the price is "
                    "falling while settled ownership rises."
                )

        persistence = metrics.get("delivery_persistence")
        required = int(self.cfg.get("delivery.persistence_sessions_required", 2))
        if persistence is not None:
            findings.append(
                f"Elevated delivery on {persistence} of the last 10 down sessions (need {required})."
            )
            components.append(band_score(persistence, [(5, 3.0), (3, 2.0), (2, 1.0), (1, -0.5), (-1e9, -2.0)]))
            if persistence <= 1 and ratio and ratio > 1.15:
                red_flags.append(
                    "The delivery spike appears on a single session, which is what one large "
                    "block deal looks like - not accumulation."
                )

        average = metrics.get("delivery_pct_avg")
        if average is not None:
            findings.append(f"Average delivery {average:.1f}% of traded volume.")
            components.append(band_score(average, [(70, 2.5), (55, 1.5), (40, 0.5), (25, -1.5), (-1e9, -3.0)]))

        turnover = metrics.get("avg_turnover_cr")
        if turnover is not None:
            findings.append(f"Average turnover Rs {turnover:.1f} crore a day.")
            if turnover < 3:
                red_flags.append(f"Thin liquidity at Rs {turnover:.1f} cr/day makes exit costly.")
                components.append(-2.0)

        score = sum(components) / len(components) if components else 0.0
        confidence = min(0.85, 0.35 + metrics.get("sessions", 0) * 0.01)

        return self.verdict(score, confidence, findings,
                            [Evidence("down_day_delivery_ratio", ratio),
                             Evidence("delivery_persistence", persistence),
                             Evidence("delivery_pct_avg", average)],
                            red_flags)


class InsiderActivityAnalyst(Analyst):
    """What the people who know most have been doing with their own money."""

    bot_id = "insider_activity_analyst"
    desk = DESK
    name = "Insider Activity Analyst"
    requires = ("insider",)

    def gather(self, ctx: Any) -> dict[str, Any]:
        frame = ctx.get("insider")
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            # NSE reachable but nothing filed is a real reading, and a
            # different one from NSE being unreachable.
            result = ctx.insider
            if result is not None and result.status.value == "not_applicable":
                return {
                    "no_disclosures": True,
                    "note": result.note or "no disclosures in the window",
                }
            return {"data_available": False, "note": ctx.note_for("insider")}

        buys = frame[frame["is_buy"]] if "is_buy" in frame else frame.iloc[0:0]
        sells = frame[frame["is_sell"]] if "is_sell" in frame else frame.iloc[0:0]
        promoters = frame[frame["is_promoter"]] if "is_promoter" in frame else frame.iloc[0:0]

        net_value = float(frame["signed_value"].sum(skipna=True)) if "signed_value" in frame else 0.0
        promoter_net = (
            float(promoters["signed_value"].sum(skipna=True))
            if "signed_value" in promoters and not promoters.empty else 0.0
        )

        market_cap = None
        fundamentals = ctx.get("fundamentals") or {}
        if fundamentals.get("market_cap"):
            market_cap = float(fundamentals["market_cap"])

        return {
            "no_disclosures": False,
            "transactions": len(frame),
            "buy_count": len(buys),
            "sell_count": len(sells),
            "net_value": net_value,
            "promoter_transactions": len(promoters),
            "promoter_net_value": promoter_net,
            "net_pct_of_mcap": (net_value / market_cap * 100.0) if market_cap else None,
        }

    def judge(self, metrics: dict[str, Any]) -> Verdict:
        if metrics.get("no_disclosures"):
            return self.verdict(
                0.0, 0.3,
                [
                    f"No insider disclosures filed in the window ({metrics['note']}).",
                    "Silence is the normal state for most companies most of the time - it is "
                    "neither reassuring nor alarming.",
                ],
                [Evidence("insider_disclosures", 0)],
            )

        findings: list[str] = []
        red_flags: list[str] = []

        net = metrics["net_value"]
        promoter_net = metrics["promoter_net_value"]
        direction = "bought" if net > 0 else "sold"

        findings.append(
            f"{metrics['transactions']} disclosures: {metrics['buy_count']} purchases and "
            f"{metrics['sell_count']} sales, netting Rs {abs(net):,.0f} {direction}."
        )

        components: list[float] = []

        share = metrics.get("net_pct_of_mcap")
        if share is not None:
            findings.append(f"That is {abs(share):.3f}% of market capitalisation.")
            components.append(band_score(share, [(0.5, 4.0), (0.1, 2.5), (0.01, 1.0),
                                                 (-0.01, 0.0), (-0.1, -1.5), (-0.5, -3.0), (-1e9, -4.5)]))
        else:
            components.append(2.0 if net > 0 else (-2.0 if net < 0 else 0.0))

        if metrics["promoter_transactions"]:
            promoter_direction = "buying" if promoter_net > 0 else "selling"
            findings.append(
                f"{metrics['promoter_transactions']} of these were promoters, net "
                f"{promoter_direction} Rs {abs(promoter_net):,.0f}."
            )
            # Promoter trades carry far more signal than an employee exercising
            # options, so they are weighted separately rather than averaged in.
            components.append(3.0 if promoter_net > 0 else -3.5)
            if promoter_net < 0:
                red_flags.append(
                    f"Promoters were net sellers of Rs {abs(promoter_net):,.0f}. The people "
                    f"with the best information are reducing."
                )
        else:
            findings.append("No promoter transactions - all disclosures were other insiders.")

        alarm = float(self.cfg.get("ownership.insider_net_sell_alarm_pct", 1.0))
        if share is not None and share <= -alarm:
            red_flags.append(f"Net insider selling of {abs(share):.2f}% exceeds the {alarm}% alarm level.")

        score = sum(components) / len(components) if components else 0.0
        confidence = min(0.8, 0.35 + metrics["transactions"] * 0.05)

        return self.verdict(score, confidence, findings,
                            [Evidence("net_insider_value", round(net, 0)),
                             Evidence("promoter_net_value", round(promoter_net, 0)),
                             Evidence("net_pct_of_mcap", None if share is None else round(share, 4))],
                            red_flags)


class FundHoldingsAnalyst(Analyst):
    """Are institutions adding or leaving, quarter on quarter."""

    bot_id = "fund_holdings_analyst"
    desk = DESK
    name = "Fund Holdings Research Analyst"
    requires = ("shareholding",)

    #: NSE's shareholding master returns one row per quarter in wide form,
    #: with promoter and public as columns rather than category rows. It does
    #: not break institutions out into FII/DII/MF - that lives in the detailed
    #: XBRL filing - so promoter direction is what this bot can actually see.
    WIDE_COLUMNS = {
        "promoter": ("pr_and_prgrp", "promoterandpromotergroup", "promoter"),
        "public": ("public_val", "public"),
        "employee_trusts": ("employeetrusts", "employee_trusts"),
    }

    def gather(self, ctx: Any) -> dict[str, Any]:
        frame = ctx.get("shareholding")
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            return {"data_available": False, "note": ctx.note_for("shareholding")}

        quarters = self._parse_wide(frame)
        shape = "wide"
        if not quarters:
            quarters = self._parse_long(frame)
            shape = "long"

        if not quarters:
            return {
                "data_available": False,
                "note": f"unrecognised shareholding shape: {list(frame.columns)[:6]}",
            }

        ordered = sorted(quarters.items(), reverse=True)
        latest = ordered[0][1]
        previous = ordered[1][1] if len(ordered) > 1 else {}
        deltas = {k: latest[k] - previous[k] for k in latest if k in previous}

        return {
            "quarters_available": len(ordered),
            "latest_quarter": ordered[0][0],
            "latest": latest,
            "deltas": deltas,
            "shape": shape,
            "has_institutional_split": any(
                k in latest for k in ("fii", "dii", "mutual_fund")
            ),
        }

    def _parse_wide(self, frame: pd.DataFrame) -> dict[str, dict[str, float]]:
        """One row per quarter, holders as columns."""
        lookup = {str(c).lower().replace(" ", "_"): c for c in frame.columns}
        date_column = lookup.get("date") or lookup.get("submissiondate")
        if date_column is None:
            return {}

        quarters: dict[str, dict[str, float]] = {}
        for _, row in frame.iterrows():
            when = str(row.get(date_column, "")).strip()
            if not when or when.lower() in ("nan", "none"):
                continue

            holders: dict[str, float] = {}
            for holder, candidates in self.WIDE_COLUMNS.items():
                for candidate in candidates:
                    column = lookup.get(candidate)
                    if column is None:
                        continue
                    value = pd.to_numeric(row.get(column), errors="coerce")
                    if pd.notna(value) and 0 <= float(value) <= 100:
                        holders[holder] = float(value)
                        break

            if holders:
                quarters[when] = holders

        return quarters

    def _parse_long(self, frame: pd.DataFrame) -> dict[str, dict[str, float]]:
        """One row per holder category, with a percentage column."""
        label_column = next(
            (c for c in frame.columns
             if any(k in str(c).lower() for k in ("category", "desc", "particular"))),
            None,
        )
        value_columns = [
            c for c in frame.columns
            if any(k in str(c).lower() for k in ("percent", "pct", "%", "holding"))
        ]
        date_column = next((c for c in frame.columns if "date" in str(c).lower()), None)

        if label_column is None or not value_columns:
            return {}

        quarters: dict[str, dict[str, float]] = {}
        for _, row in frame.iterrows():
            label = str(row.get(label_column, "")).lower()
            when = str(row.get(date_column, "unknown")) if date_column else "unknown"

            holder = next(
                (name for name, patterns in HOLDER_PATTERNS.items()
                 if any(p in label for p in patterns)),
                None,
            )
            if holder is None:
                continue

            for column in value_columns:
                value = pd.to_numeric(row.get(column), errors="coerce")
                if pd.notna(value) and 0 <= float(value) <= 100:
                    quarters.setdefault(when, {})[holder] = float(value)
                    break

        return quarters

    def judge(self, metrics: dict[str, Any]) -> Verdict:
        latest = metrics["latest"]
        deltas = metrics.get("deltas") or {}

        findings: list[str] = []
        components: list[float] = []
        red_flags: list[str] = []

        holdings = ", ".join(
            f"{name.replace('_', ' ')} {value:.1f}%" for name, value in sorted(latest.items())
        )
        findings.append(f"Latest filing ({metrics['latest_quarter']}): {holdings}.")

        if not deltas:
            findings.append(
                "Only one quarter is available, so the direction of institutional "
                "flow cannot be established."
            )
            return self.verdict(0.0, 0.2, findings, [Evidence("quarters", metrics["quarters_available"])])

        findings.append(
            "Quarter on quarter: "
            + ", ".join(f"{k.replace('_', ' ')} {v:+.2f}pp" for k, v in sorted(deltas.items()))
            + "."
        )

        institutional: float | None = None
        if metrics.get("has_institutional_split"):
            institutional = sum(deltas.get(k, 0.0) for k in ("fii", "dii", "mutual_fund"))
            components.append(band_score(institutional, [(2.0, 4.0), (0.5, 2.5), (0.1, 1.0),
                                                         (-0.1, 0.0), (-0.5, -1.5), (-2.0, -3.0), (-1e9, -4.0)]))

            alarm = float(self.cfg.get("ownership.institutional_exit_alarm_pct", 2.0))
            if institutional <= -alarm:
                red_flags.append(
                    f"Institutions cut their combined holding by {abs(institutional):.2f} percentage "
                    f"points in a quarter."
                )
            elif institutional > 0:
                findings.append(
                    f"Institutions added {institutional:+.2f} points in aggregate while the price fell - "
                    f"the money with research behind it is buying this decline."
                )
        else:
            # The summary filing gives promoter and public only. Public rising
            # is the mirror of promoters falling, not evidence about funds -
            # so it is reported rather than scored as institutional flow.
            findings.append(
                "This filing reports promoter and public holdings only; the FII, DII and "
                "mutual fund split lives in the detailed XBRL and is not read here, so "
                "institutional direction is not measured."
            )

        promoter_delta = deltas.get("promoter")
        if promoter_delta is not None:
            if promoter_delta < -1.0:
                components.append(-3.0)
                red_flags.append(f"Promoter holding fell {abs(promoter_delta):.2f} points.")
            elif promoter_delta > 0.5:
                components.append(2.5)
                findings.append(f"Promoters increased their stake by {promoter_delta:+.2f} points.")

        promoter_level = latest.get("promoter")
        if promoter_level is not None and promoter_level < 30:
            findings.append(
                f"Promoter holding is {promoter_level:.1f}%, which is low - there is less "
                f"skin in the game than usual."
            )
            components.append(-1.0)

        score = sum(components) / len(components) if components else 0.0
        confidence = min(0.8, 0.3 + metrics["quarters_available"] * 0.1)

        return self.verdict(score, confidence, findings,
                            [Evidence("institutional_delta_pp",
                                      None if institutional is None else round(institutional, 2)),
                             Evidence("promoter_delta_pp",
                                      None if promoter_delta is None else round(promoter_delta, 2))],
                            red_flags)


class ShortSellerAnalyst(Analyst):
    """Signs of stressed positioning: F&O bans, and large one-off deals."""

    bot_id = "short_seller_analyst"
    desk = DESK
    name = "Short-Seller Research Analyst"
    requires = ("surveillance", "deals")

    def gather(self, ctx: Any) -> dict[str, Any]:
        flags = ctx.get("surveillance") or {}
        deals = ctx.get("deals")

        deal_summary: dict[str, Any] = {"count": 0, "net_quantity": 0.0, "sellers": 0, "buyers": 0}
        if isinstance(deals, pd.DataFrame) and not deals.empty and "side" in deals.columns:
            sides = deals["side"].astype(str).str.upper()
            buys = deals[sides.str.startswith("B")]
            sells = deals[sides.str.startswith("S")]
            deal_summary = {
                "count": len(deals),
                "buyers": len(buys),
                "sellers": len(sells),
                "net_quantity": float(buys["quantity"].sum(skipna=True) - sells["quantity"].sum(skipna=True))
                if "quantity" in deals.columns else 0.0,
            }

        return {
            "in_fo_ban": bool(flags.get("in_fo_ban")),
            "asm_checked": bool(flags.get("asm_checked")),
            "deals": deal_summary,
            # NSE's short-selling endpoint returns 503; this is recorded so
            # the Validation Analyst can count it as a known blind spot rather
            # than an unexplained gap.
            "short_data_available": False,
        }

    def judge(self, metrics: dict[str, Any]) -> Verdict:
        findings: list[str] = []
        components: list[float] = []
        red_flags: list[str] = []

        if metrics["in_fo_ban"]:
            components.append(-4.0)
            red_flags.append(
                "The stock is in the F&O ban period, which means open interest has hit "
                "95% of market-wide limits - positioning is stressed."
            )
            findings.append("Currently banned from F&O trading.")
        else:
            components.append(0.5)
            findings.append("Not in today's F&O ban list.")

        deals = metrics["deals"]
        if deals["count"]:
            direction = "net bought" if deals["net_quantity"] > 0 else "net sold"
            findings.append(
                f"{deals['count']} bulk or block deals today ({deals['buyers']} buy, "
                f"{deals['sellers']} sell), {direction} {abs(deals['net_quantity']):,.0f} shares."
            )
            components.append(1.5 if deals["net_quantity"] > 0 else -1.5)
            findings.append(
                "Large one-off deals also explain delivery spikes, so this reading should be "
                "set against the volume desk's."
            )
        else:
            findings.append("No bulk or block deals reported today.")

        findings.append(
            "NSE's short-selling report is unreachable (503), so genuine short interest "
            "is not measured here. This is a known blind spot, not an all-clear."
        )
        if not metrics["asm_checked"]:
            findings.append("ASM and GSM surveillance lists are also unreachable.")

        score = sum(components) / len(components) if components else 0.0
        # Deliberately low: two of the three inputs this bot wants are missing.
        confidence = 0.3

        return self.verdict(score, confidence, findings,
                            [Evidence("in_fo_ban", metrics["in_fo_ban"]),
                             Evidence("bulk_block_deals", deals["count"]),
                             Evidence("short_selling_data", "unavailable")],
                            red_flags)


ANALYSTS = (
    MarketVolumeIntelligenceAnalyst,
    InsiderActivityAnalyst,
    FundHoldingsAnalyst,
    ShortSellerAnalyst,
)
