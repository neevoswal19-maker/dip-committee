"""Equity Research Lead's three analysts.

All arithmetic, and all reusing work already built and tested:
`fundamentals.get_fundamentals` and `compute_forensics` do the computation,
these bots do the judging.

The Financial Forensics Analyst holds the committee's only veto. That
asymmetry is deliberate - every other bot trades off against the rest, but
accounting fraud is not a factor to be outweighed by a good chart. The failure
it guards against is permanent loss of capital rather than underperformance.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from src.agents import lexicon
from src.agents.base import Analyst, band_score
from src.agents.schemas import Evidence, Verdict

DESK = "equity"


class FundamentalResearchAnalyst(Analyst):
    """Is the business worth owning. Whether it is cheap is the Valuation Analyst's call."""

    bot_id = "fundamental_research_analyst"
    desk = DESK
    name = "Fundamental Research Analyst"
    requires = ("fundamentals",)

    def gather(self, ctx: Any) -> dict[str, Any]:
        metrics = ctx.get("fundamentals")
        if not metrics:
            return {"data_available": False, "note": "no fundamentals"}

        known = sum(
            1 for key in ("roe_pct", "debt_to_equity", "revenue_cagr_3y_pct",
                          "pe_trailing", "profit_margin_pct")
            if metrics.get(key) is not None
        )
        if known < 2:
            return {
                "data_available": False,
                "note": f"only {known} of 5 core metrics available - too thin to judge",
            }

        return {**metrics, "known_metrics": known}

    def judge(self, metrics: dict[str, Any]) -> Verdict:
        findings: list[str] = []
        red_flags: list[str] = []
        components: list[float] = []

        roe = metrics.get("roe_pct")
        if roe is not None:
            score = band_score(roe, [(25, 4.5), (20, 3.5), (15, 2.0), (12, 1.0), (8, -1.0), (0, -2.5), (-1e9, -4.0)])
            components.append(score)
            findings.append(f"ROE {roe:.1f}%.")

        debt = metrics.get("debt_to_equity")
        sector = (metrics.get("sector") or "").strip()
        exempt = set(self.cfg.get("quality_gate.debt_exempt_sectors", []) or [])
        if debt is not None and sector not in exempt:
            score = band_score(debt * -1, [(-0.2, 3.0), (-0.5, 2.0), (-1.0, 0.5), (-1.5, -1.0), (-1e9, -3.5)])
            components.append(score)
            findings.append(f"Debt to equity {debt:.2f}.")
            if debt > 2.0:
                red_flags.append(f"Leverage is high at {debt:.2f}x equity.")
        elif sector in exempt:
            findings.append(f"Leverage not scored: {sector} is a lending business where D/E is not comparable.")

        growth = metrics.get("revenue_cagr_3y_pct")
        if growth is not None:
            components.append(band_score(growth, [(20, 4.0), (12, 2.5), (6, 1.0), (0, -0.5), (-1e9, -3.0)]))
            findings.append(f"Revenue CAGR over three years {growth:+.1f}%.")

        profit_growth = metrics.get("profit_cagr_3y_pct")
        if profit_growth is not None and growth is not None:
            if profit_growth < growth - 5:
                findings.append(
                    f"Profit is growing at {profit_growth:+.1f}% against revenue at {growth:+.1f}% - "
                    f"margins are being given away to buy that growth."
                )
                components.append(-1.0)
            elif profit_growth > growth + 5:
                findings.append(f"Profit ({profit_growth:+.1f}%) outpacing revenue - operating leverage.")
                components.append(1.5)

        # Valuation is the Valuation Analyst's job; scoring P/E here as well
        # would count it twice in the equity desk.

        profitable = metrics.get("profitable_years_of_4")
        if profitable is not None:
            if profitable == 4:
                components.append(1.5)
            elif profitable <= 2:
                components.append(-3.0)
                red_flags.append(f"Profitable in only {profitable} of the last 4 years.")
            findings.append(f"Profitable in {profitable} of the last 4 years.")

        score = sum(components) / len(components) if components else 0.0
        confidence = min(0.9, 0.35 + metrics["known_metrics"] * 0.1)

        return self.verdict(
            score, confidence, findings,
            [
                Evidence("roe_pct", roe), Evidence("debt_to_equity", debt),
                Evidence("revenue_cagr_3y_pct", growth), Evidence("pe_trailing", metrics.get("pe_trailing")),
            ],
            red_flags,
        )


class FinancialForensicsAnalyst(Analyst):
    """Do the accounts hold together. Holds the committee's veto."""

    bot_id = "financial_forensics_analyst"
    desk = DESK
    name = "Financial Forensics Analyst"
    requires = ("fundamentals",)

    def gather(self, ctx: Any) -> dict[str, Any]:
        metrics = ctx.get("fundamentals") or {}
        forensics = metrics.get("forensics") or {}

        if not forensics or forensics.get("data_years", 0) < 2:
            return {
                "data_available": False,
                "note": "fewer than two years of statements - nothing to compare against",
            }

        # Pledge changes surface in filings rather than statements.
        pledge_mentions = 0
        announcements = ctx.get("announcements")
        if isinstance(announcements, pd.DataFrame) and not announcements.empty:
            for _, row in announcements.iterrows():
                text = " ".join(
                    str(row.get(c, "")) for c in ("desc", "subject", "smIndustry")
                )
                name, _ = lexicon.classify_event(text)
                if name in ("pledge", "governance"):
                    pledge_mentions += 1

        return {**forensics, "governance_filings": pledge_mentions}

    def judge(self, metrics: dict[str, Any]) -> Verdict:
        findings: list[str] = []
        red_flags: list[str] = []
        components: list[float] = []
        critical = False

        cfo_ratio = metrics.get("cfo_to_pat_latest")
        years_below = metrics.get("years_cfo_below_pat")
        if cfo_ratio is not None:
            components.append(band_score(cfo_ratio, [(1.5, 3.0), (1.0, 1.5), (0.8, -0.5), (0.5, -2.5), (-1e9, -4.0)]))
            findings.append(
                f"Operating cash flow is {cfo_ratio:.2f}x reported profit "
                f"(three-year average {metrics.get('cfo_to_pat_avg_3y', 'n/a')})."
            )
            if years_below is not None and years_below >= 3:
                critical = True
                red_flags.append(
                    f"Cash flow has trailed reported profit in {years_below} of the last 4 years. "
                    f"One year is working-capital timing; a run of years is a pattern."
                )
                components.append(-4.0)
            elif years_below == 2:
                red_flags.append("Cash flow below profit in 2 of the last 4 years - worth watching.")

        gap = metrics.get("receivables_vs_sales_gap_pct")
        if gap is not None:
            components.append(band_score(gap * -1, [(10, 1.5), (0, 0.5), (-15, -1.0), (-30, -3.0), (-1e9, -4.5)]))
            findings.append(f"Receivables grew {gap:+.1f} percentage points faster than sales.")
            if gap > 35:
                critical = True
                red_flags.append(
                    f"Receivables outpacing sales by {gap:.0f} points - revenue may be booked "
                    f"on terms that are not being collected."
                )

        days = metrics.get("receivable_days")
        if days is not None:
            findings.append(f"Receivable days {days:.0f}.")
            if days > 180:
                red_flags.append(f"{days:.0f} days of receivables outstanding is very long.")
                components.append(-2.0)

        debt_growth = metrics.get("debt_growth_yoy_pct")
        if debt_growth is not None:
            findings.append(f"Debt changed {debt_growth:+.1f}% year on year.")
            if debt_growth > 40:
                components.append(-2.5)
                red_flags.append(f"Debt up {debt_growth:.0f}% in a year.")
            elif debt_growth < -10:
                components.append(1.5)

        inventory_gap = metrics.get("inventory_vs_sales_gap_pct")
        if inventory_gap is not None and inventory_gap > 30:
            components.append(-1.5)
            findings.append(
                f"Inventory growing {inventory_gap:.0f} points faster than sales, which "
                f"sometimes precedes a write-down."
            )

        direction = metrics.get("margin_direction")
        if direction:
            findings.append(f"Net margin trend: {metrics.get('net_margin_trend_pct')} ({direction}).")
            components.append({"expanding": 1.5, "flat": 0.0, "compressing": -1.5}.get(direction, 0.0))

        if metrics.get("governance_filings"):
            findings.append(
                f"{metrics['governance_filings']} filings touching pledge or governance in the "
                f"last 90 days - read those directly."
            )
            components.append(-1.0)

        score = sum(components) / len(components) if components else 0.0
        confidence = min(0.9, 0.4 + metrics.get("data_years", 0) * 0.12)

        if critical:
            findings.insert(
                0,
                "VETO. A critical accounting flag overrides the rest of the committee: "
                "the risk here is permanent loss of capital, not underperformance.",
            )

        return self.verdict(score, confidence, findings,
                            [Evidence("cfo_to_pat_latest", cfo_ratio),
                             Evidence("years_cfo_below_pat", years_below),
                             Evidence("receivables_vs_sales_gap_pct", gap)],
                            red_flags, veto=critical)


class PreEarningsAnalyst(Analyst):
    """How close are results, and how does this stock usually behave around them."""

    bot_id = "pre_earnings_analyst"
    desk = DESK
    name = "Pre-Earnings Intelligence Analyst"
    requires = ("announcements", "price_frame")

    def gather(self, ctx: Any) -> dict[str, Any]:
        frame = ctx.price_frame
        announcements = ctx.get("announcements")

        if frame is None or frame.empty:
            return {"data_available": False, "note": "no price history"}

        result_dates: list[datetime] = []
        if isinstance(announcements, pd.DataFrame) and not announcements.empty:
            for _, row in announcements.iterrows():
                text = " ".join(str(row.get(c, "")) for c in ("desc", "subject"))
                name, _ = lexicon.classify_event(text)
                if name == "results":
                    for column in ("an_dt", "sort_date", "date"):
                        value = row.get(column)
                        if value and str(value).lower() not in ("nan", "none"):
                            parsed = pd.to_datetime(str(value), errors="coerce", dayfirst=True)
                            if pd.notna(parsed):
                                result_dates.append(parsed.to_pydatetime())
                                break

        # Post-result drift: how the stock moved over the five sessions after
        # each past result. Says more about this stock's own behaviour than
        # any general rule about earnings does.
        drifts: list[float] = []
        returns = frame["close"]
        for when in result_dates:
            stamp = pd.Timestamp(when.date())
            after = returns.index[returns.index >= stamp]
            if len(after) < 6:
                continue
            start = float(returns.loc[after[0]])
            end = float(returns.loc[after[5]])
            if start > 0:
                drifts.append((end - start) / start * 100.0)

        last_result = max(result_dates) if result_dates else None
        days_since = (datetime.now() - last_result).days if last_result else None

        # Indian listed companies report quarterly, so the next is roughly
        # 90 days after the last.
        days_to_next = (90 - days_since) if days_since is not None and days_since < 90 else None

        return {
            "result_filings": len(result_dates),
            "days_since_last": days_since,
            "days_to_next_estimate": days_to_next,
            "drift_samples": len(drifts),
            "mean_drift_pct": sum(drifts) / len(drifts) if drifts else None,
            "positive_drifts": sum(1 for d in drifts if d > 0),
        }

    def judge(self, metrics: dict[str, Any]) -> Verdict:
        findings: list[str] = []
        score = 0.0

        days_since = metrics.get("days_since_last")
        if days_since is not None:
            findings.append(f"Last results filing was {days_since} days ago.")
        else:
            findings.append("No results filing found in the last 90 days.")

        days_to_next = metrics.get("days_to_next_estimate")
        if days_to_next is not None and days_to_next <= 21:
            findings.append(
                f"Results are roughly {days_to_next} days away on a quarterly cadence. "
                f"Buying immediately before a result is a bet on the result, not on the dip."
            )
            score -= 1.0

        drift = metrics.get("mean_drift_pct")
        samples = metrics.get("drift_samples", 0)
        if drift is not None and samples >= 2:
            findings.append(
                f"Over {samples} past results, the five sessions after averaged {drift:+.1f}% "
                f"({metrics['positive_drifts']} of {samples} positive)."
            )
            score += max(-2.0, min(2.0, drift * 0.3))
        else:
            findings.append("Too few past results in the window to measure post-result drift.")

        confidence = 0.2 + min(0.4, samples * 0.12)
        return self.verdict(score, confidence, findings,
                            [Evidence("days_since_last_result", days_since),
                             Evidence("mean_post_result_drift_pct",
                                      None if drift is None else round(drift, 2))])


class ValuationAnalyst(Analyst):
    """Is the price cheap - against the company's own past, and against its peers.

    A single P/E scale for every company makes banks look permanently cheap
    and consumer-goods makers permanently dear. So the stock is judged on
    the right measure for its kind of business, and against two references:

    * **Its own history**: today's multiple against its median at the last
      several fiscal year-ends. Cheaper than it usually is?
    * **Its sector**: against the sector's median across the Nifty 500
      (refreshed weekly). Cheaper than its peers?

    Lenders are judged on price to book, because earnings multiples mislead
    for them. Everyone else on P/E, plus PEG (P/E over profit growth, so a
    fast grower is not punished for a higher multiple) and free-cash-flow
    yield (the cash the business actually produces for its price).
    """

    bot_id = "valuation_analyst"
    desk = DESK
    name = "Valuation Analyst"
    requires = ("fundamentals",)

    #: current / reference -> score. Below 0.7 of its usual multiple is cheap.
    RATIO_BANDS = [(-0.70, 4.0), (-0.85, 2.5), (-1.00, 1.0), (-1.15, -0.5), (-1.35, -2.0), (-1e9, -3.5)]

    def gather(self, ctx: Any) -> dict[str, Any]:
        from statistics import median

        from src.data import valuations

        f = ctx.get("fundamentals")
        if not f:
            return {"data_available": False, "note": "no fundamentals"}

        sector = (f.get("sector") or "").strip()
        lender = sector in set(self.cfg.get("quality_gate.debt_exempt_sectors", []) or [])
        basis = "pb" if lender else "pe"

        frame = ctx.price_frame
        closes = frame["close"] if frame is not None and not frame.empty else None
        price = float(closes.iloc[-1]) if closes is not None else None

        def close_on(day: str) -> float | None:
            if closes is None:
                return None
            before = closes[closes.index <= pd.Timestamp(day)]
            # Only if the price history actually reaches back that far.
            if before.empty or (pd.Timestamp(day) - before.index[-1]).days > 10:
                return None
            return float(before.iloc[-1])

        per_share = f.get("bvps_history" if lender else "eps_history") or []
        history = []
        for day, value in per_share:
            px = close_on(day)
            if px is not None and value and value > 0:
                history.append(px / float(value))

        if lender:
            current = f.get("price_to_book")
            if (not current or current <= 0) and price and per_share and per_share[0][1] > 0:
                current = price / per_share[0][1]
        else:
            current = f.get("pe_trailing")
            if (not current or current <= 0) and price and per_share and per_share[0][1] > 0:
                current = price / per_share[0][1]
        current = float(current) if current and current > 0 else None

        loss_making = (not lender and current is None and bool(per_share) and per_share[0][1] <= 0)

        peers = valuations.sector_medians(sector)
        growth = f.get("profit_cagr_3y_pct")
        peg = current / growth if (not lender and current and growth and growth > 0) else None
        fcf_yield = None if lender else f.get("fcf_yield_pct")

        if current is None and fcf_yield is None and not loss_making:
            return {"data_available": False, "note": "no usable valuation figures"}

        return {
            "basis": basis, "sector": sector, "current": current,
            "own_median": median(history) if len(history) >= 3 else None,
            "own_years": len(history),
            "sector_median": (peers.get(basis) or {}).get("median"),
            "sector_peers": (peers.get(basis) or {}).get("n"),
            "peg": peg, "profit_growth": growth,
            "fcf_yield": fcf_yield,
            "sector_fcf_yield": None if lender else (peers.get("fcf_yield") or {}).get("median"),
            "loss_making": loss_making,
        }

    def judge(self, metrics: dict[str, Any]) -> Verdict:
        label_name = "P/B" if metrics["basis"] == "pb" else "P/E"
        current = metrics["current"]
        components: list[float] = []
        findings: list[str] = []
        refs: list[str] = []

        if metrics["loss_making"]:
            components.append(-2.0)
            findings.append("Loss-making in the latest year, so there is no meaningful P/E.")

        own = metrics["own_median"]
        if current and own:
            components.append(band_score(-(current / own), self.RATIO_BANDS))
            refs.append(f"its usual {own:.1f}")
            findings.append(f"{label_name} {current:.1f} against its median of {own:.1f} at the last "
                            f"{metrics['own_years']} year-ends.")

        peer = metrics["sector_median"]
        if current and peer:
            components.append(band_score(-(current / peer), self.RATIO_BANDS))
            refs.append(f"sector {peer:.1f}")
            findings.append(f"{label_name} {current:.1f} against the {metrics['sector']} median of "
                            f"{peer:.1f} ({metrics['sector_peers']} companies).")
        elif current and not peer:
            findings.append("No sector median available yet, so it is not compared with peers.")

        peg = metrics["peg"]
        if peg is not None:
            components.append(band_score(-peg, [(-0.8, 3.0), (-1.2, 1.5), (-2.0, 0.0), (-3.0, -1.5), (-1e9, -3.0)]))
            findings.append(f"PEG {peg:.2f}: P/E {current:.1f} over {metrics['profit_growth']:.1f}% "
                            f"yearly profit growth.")

        fcf = metrics["fcf_yield"]
        if fcf is not None:
            components.append(band_score(fcf, [(6.0, 3.0), (4.0, 2.0), (2.0, 0.5), (0.0, -0.5), (-1e9, -2.0)]))
            sector_fcf = metrics["sector_fcf_yield"]
            findings.append(f"Free-cash-flow yield {fcf:.1f}%"
                            + (f" against a sector median of {sector_fcf:.1f}%." if sector_fcf is not None else "."))

        score = sum(components) / len(components) if components else 0.0
        label = "CHEAP" if score >= 1.5 else ("EXPENSIVE" if score <= -1.5 else "FAIR")
        if metrics["loss_making"] and not current:
            label = "LOSS-MAKING"
        if current:
            headline = f"Valuation: {label} - {label_name} {current:.1f}" + (
                f" vs {' and '.join(refs)}." if refs else ".")
        else:
            headline = f"Valuation: {label}."
        findings.insert(0, headline)

        return self.verdict(
            score, min(0.8, 0.3 + 0.12 * len(components)), findings,
            [
                Evidence("valuation_label", label),
                Evidence("basis", label_name),
                Evidence("current", round(current, 2) if current else None),
                Evidence("own_median", round(own, 2) if own else None),
                Evidence("sector_median", round(peer, 2) if peer else None),
                Evidence("peg", round(peg, 2) if peg is not None else None),
                Evidence("fcf_yield_pct", round(fcf, 2) if fcf is not None else None),
            ],
        )


ANALYSTS = (FundamentalResearchAnalyst, FinancialForensicsAnalyst, PreEarningsAnalyst, ValuationAnalyst)
