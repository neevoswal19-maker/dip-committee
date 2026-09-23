"""Position sizing: how much to actually invest.

Six steps, in order. Each one can only ever reduce the size, never raise it,
so the final number is the most conservative answer any single constraint
would give.

  1. Estimate the edge (p, b) from the closed-trade ledger, bucketed by
     conviction. Cold-start from the backtest until enough trades exist.
  2. Raw Kelly. A non-positive result is NO BUY regardless of the report.
  3. Conviction picks the Kelly fraction - this is the aggressive /
     balanced / conservative call.
  4. ATR overlay. Take the smaller of the Kelly rupees and the ATR rupees.
  5. Portfolio constraints: per-stock cap, sector cap, position count,
     total heat.
  6. Split into staged entry tranches, because dips deepen.

Full Kelly is never used. Half Kelly gives up roughly a quarter of the
theoretical growth rate to halve worst-case drawdown, which is the correct
trade when `p` is estimated from a few dozen trades rather than known. The
formula assumes you know your edge exactly; you do not, and the fraction is
what pays for that uncertainty.
"""

from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

log = logging.getLogger(__name__)

Recommendation = Literal["AGGRESSIVE", "BALANCED", "CONSERVATIVE", "NO_BUY"]
BAND_ORDER = ("aggressive", "balanced", "conservative")


@dataclass
class EdgeEstimate:
    """Win probability and payoff ratio, with its provenance.

    `source` matters as much as the numbers. A size derived from 12 trades
    is a guess wearing a decimal point, and the report says so rather than
    presenting it with the same authority as one built on 200.
    """

    win_probability: float
    payoff_ratio: float
    sample_size: int
    source: Literal["ledger", "cold_start", "blended"]
    band: str

    @property
    def expectancy(self) -> float:
        """Expected return per rupee risked. Negative means no edge at all."""
        return self.win_probability * self.payoff_ratio - (1.0 - self.win_probability)


@dataclass
class Tranche:
    sequence: int
    pct_of_position: float
    trigger: str
    trigger_price: float | None
    value: float
    shares: int
    description: str


@dataclass
class SizingDecision:
    """The complete sizing answer, with every step shown.

    Deliberately verbose: the point is not just the number but why it is
    that number, so the report can say "bound by the ATR overlay, not by
    Kelly" instead of asking you to take it on faith.
    """

    symbol: str
    recommendation: Recommendation
    conviction: float

    total_value: float = 0.0
    total_shares: int = 0
    pct_of_capital: float = 0.0
    risk_amount: float = 0.0
    risk_pct_of_capital: float = 0.0
    stop_price: float | None = None

    edge: EdgeEstimate | None = None
    raw_kelly: float = 0.0
    kelly_fraction_applied: float = 0.0
    kelly_value: float = 0.0
    atr_value: float = 0.0
    binding_constraint: str = ""

    tranches: list[Tranche] = field(default_factory=list)
    rejections: list[str] = field(default_factory=list)
    adjustments: list[str] = field(default_factory=list)
    narrative: str = ""

    @property
    def is_buy(self) -> bool:
        return self.recommendation != "NO_BUY" and self.total_value > 0

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["is_buy"] = self.is_buy
        return payload


@dataclass
class PortfolioState:
    """What is already held, so sizing can respect portfolio-level limits."""

    capital: float
    open_positions: int = 0
    # symbol -> current market value
    holdings: dict[str, float] = field(default_factory=dict)
    # sector -> total market value
    sector_exposure: dict[str, float] = field(default_factory=dict)
    # Sum of open risk (entry minus stop, times quantity) across positions
    open_risk: float = 0.0

    @property
    def deployed(self) -> float:
        return sum(self.holdings.values())

    @property
    def cash(self) -> float:
        return max(0.0, self.capital - self.deployed)

    def sector_pct(self, sector: str | None) -> float:
        if not sector or self.capital <= 0:
            return 0.0
        return self.sector_exposure.get(sector, 0.0) / self.capital * 100.0

    @property
    def heat_pct(self) -> float:
        if self.capital <= 0:
            return 0.0
        return self.open_risk / self.capital * 100.0


# --- Step 1: estimate the edge ----------------------------------------------


def estimate_edge(
    band: str,
    cfg: Any,
    closed_trades: list[dict[str, Any]] | None = None,
) -> EdgeEstimate:
    """Win rate and payoff ratio for a conviction band.

    Measured from the system's own closed trades where there are enough of
    them, from the backtest's cold-start figures where there are not, and
    blended in between so the estimate migrates gradually from prior to
    evidence rather than lurching on the day the 20th trade closes.
    """
    cold = cfg.get(f"sizing.cold_start.{band}", {}) or {}
    cold_p = float(cold.get("p", 0.55))
    cold_b = float(cold.get("b", 1.6))
    minimum = int(cfg.get("sizing.min_trades_for_live_estimate", 20))

    relevant = [t for t in (closed_trades or []) if t.get("conviction_band") == band]
    n = len(relevant)

    if n == 0:
        return EdgeEstimate(cold_p, cold_b, 0, "cold_start", band)

    wins = [t for t in relevant if t.get("return_pct", 0) > 0]
    losses = [t for t in relevant if t.get("return_pct", 0) <= 0]

    if not wins or not losses:
        # Every trade one way tells us nothing reliable about the ratio.
        return EdgeEstimate(cold_p, cold_b, n, "cold_start", band)

    observed_p = len(wins) / n
    avg_win = sum(t["return_pct"] for t in wins) / len(wins)
    avg_loss = abs(sum(t["return_pct"] for t in losses) / len(losses))
    observed_b = avg_win / avg_loss if avg_loss > 0 else cold_b

    if n >= minimum:
        return EdgeEstimate(observed_p, observed_b, n, "ledger", band)

    # Linear blend: at n=0 pure prior, at n=minimum pure observation.
    weight = n / minimum
    return EdgeEstimate(
        win_probability=cold_p * (1 - weight) + observed_p * weight,
        payoff_ratio=cold_b * (1 - weight) + observed_b * weight,
        sample_size=n,
        source="blended",
        band=band,
    )


# --- Step 2: raw Kelly ------------------------------------------------------


def kelly_fraction(win_probability: float, payoff_ratio: float) -> float:
    """f* = (b*p - q) / b

    Returns 0.0 rather than a negative number when there is no edge. A
    negative Kelly technically says "take the other side", which is not an
    option here - we only ever buy - so it collapses to "do not trade".
    """
    if payoff_ratio <= 0:
        return 0.0
    p = max(0.0, min(1.0, win_probability))
    q = 1.0 - p
    f = (payoff_ratio * p - q) / payoff_ratio
    return max(0.0, f)


# --- Step 3: conviction picks the band --------------------------------------


def select_band(
    conviction: float,
    cfg: Any,
    *,
    has_red_flags: bool = False,
    bull_bear_agree: bool = True,
    win_probability: float | None = None,
) -> tuple[str | None, list[str]]:
    """Choose aggressive / balanced / conservative, or none at all.

    Walks from the most aggressive band down, taking the first whose
    conditions are met. A stock that qualifies on conviction for AGGRESSIVE
    but carries a red flag does not get rejected - it steps down to
    BALANCED, which is the honest answer: still worth owning, not worth
    betting heavily on.
    """
    notes: list[str] = []

    for band in BAND_ORDER:
        spec = cfg.get(f"sizing.bands.{band}", {}) or {}
        threshold = float(spec.get("min_conviction", 101))

        if conviction < threshold:
            continue

        if spec.get("require_no_red_flags") and has_red_flags:
            notes.append(f"conviction {conviction:.0f} reaches {band}, but red flags force a step down")
            continue

        if spec.get("require_bull_bear_agreement") and not bull_bear_agree:
            notes.append(f"conviction {conviction:.0f} reaches {band}, but bull and bear analysts disagree")
            continue

        floor = spec.get("min_win_probability")
        if floor is not None and win_probability is not None and win_probability < float(floor):
            notes.append(
                f"{band} needs an estimated win rate of {float(floor):.0%}, "
                f"and the measured rate is {win_probability:.0%}"
            )
            continue

        return band, notes

    lowest = float(cfg.get(f"sizing.bands.{BAND_ORDER[-1]}.min_conviction", 45))
    notes.append(f"conviction {conviction:.0f} is below the {lowest:.0f} floor for any position")
    return None, notes


# --- The full calculation ---------------------------------------------------


def size_position(
    *,
    symbol: str,
    conviction: float,
    price: float,
    atr: float,
    cfg: Any,
    portfolio: PortfolioState,
    sector: str | None = None,
    has_red_flags: bool = False,
    forensics_veto: bool = False,
    bull_bear_agree: bool = True,
    closed_trades: list[dict[str, Any]] | None = None,
) -> SizingDecision:
    """Run all six steps and return the decision with its full reasoning."""

    decision = SizingDecision(symbol=symbol, recommendation="NO_BUY", conviction=conviction)

    # A critical forensic finding ends the conversation. It is the one input
    # that is not weighed against anything else, because the failure mode it
    # guards against is permanent loss of capital, not underperformance.
    if forensics_veto:
        decision.rejections.append("Financial Forensics veto: accounting red flag overrides every other signal")
        decision.narrative = "NO BUY. The forensics desk raised a critical flag, which overrides the rest of the committee."
        return decision

    if price <= 0:
        decision.rejections.append("no valid price")
        return decision

    # --- Step 3 first, because the band decides which edge estimate applies
    provisional_band, band_notes = select_band(
        conviction, cfg, has_red_flags=has_red_flags, bull_bear_agree=bull_bear_agree
    )
    decision.adjustments.extend(band_notes)

    if provisional_band is None:
        decision.rejections.append(band_notes[-1] if band_notes else "conviction too low")
        decision.narrative = f"NO BUY. Conviction of {conviction:.0f} does not clear the floor for a position."
        return decision

    # --- Step 1
    edge = estimate_edge(provisional_band, cfg, closed_trades)
    decision.edge = edge

    # Re-check the band now that the win probability is known: the
    # conservative band has a floor on it.
    band, recheck_notes = select_band(
        conviction, cfg,
        has_red_flags=has_red_flags,
        bull_bear_agree=bull_bear_agree,
        win_probability=edge.win_probability,
    )
    for note in recheck_notes:
        if note not in decision.adjustments:
            decision.adjustments.append(note)

    if band is None:
        decision.rejections.append(
            f"estimated win rate {edge.win_probability:.0%} is too low to justify even a starter position"
        )
        decision.narrative = (
            f"NO BUY. Conviction of {conviction:.0f} would allow a starter position, but the measured "
            f"win rate of {edge.win_probability:.0%} does not clear the floor."
        )
        return decision

    if band != provisional_band:
        edge = estimate_edge(band, cfg, closed_trades)
        decision.edge = edge

    spec = cfg.get(f"sizing.bands.{band}", {}) or {}

    # --- Step 2
    raw = kelly_fraction(edge.win_probability, edge.payoff_ratio)
    decision.raw_kelly = raw

    if raw <= 0:
        decision.rejections.append(
            f"Kelly is non-positive (win rate {edge.win_probability:.0%}, payoff {edge.payoff_ratio:.2f}) - no edge to bet on"
        )
        decision.narrative = (
            f"NO BUY. However the report reads, the measured edge for the {band} band is not positive: "
            f"a {edge.win_probability:.0%} win rate at a {edge.payoff_ratio:.2f} payoff has negative expectancy."
        )
        return decision

    # --- Step 3 applied
    fraction = float(spec.get("kelly_fraction", 0.25))
    decision.kelly_fraction_applied = fraction
    kelly_capital_pct = raw * fraction
    decision.kelly_value = portfolio.capital * kelly_capital_pct

    # --- Step 4: ATR overlay
    risk_pct = float(spec.get("risk_per_position_pct", 1.5))
    multiplier = float(cfg.get("sizing.atr_stop_multiplier", 2.5))
    risk_amount = portfolio.capital * risk_pct / 100.0

    if atr and atr > 0:
        stop_distance = atr * multiplier
        decision.stop_price = round(price - stop_distance, 2)
        atr_shares = risk_amount / stop_distance
        decision.atr_value = atr_shares * price
    else:
        # No ATR means no volatility-aware ceiling, so fall back to the
        # per-stock cap rather than letting Kelly run unchecked.
        decision.atr_value = portfolio.capital * float(spec.get("max_portfolio_pct", 12.0)) / 100.0
        decision.adjustments.append("no ATR available; volatility overlay fell back to the per-stock cap")

    if decision.kelly_value <= decision.atr_value:
        target = decision.kelly_value
        decision.binding_constraint = "Kelly"
    else:
        target = decision.atr_value
        decision.binding_constraint = "ATR volatility overlay"

    # --- Step 5: portfolio constraints
    constraints = cfg.get("sizing.constraints", {}) or {}

    max_positions = int(constraints.get("max_positions", 15))
    if portfolio.open_positions >= max_positions:
        decision.rejections.append(f"already holding {portfolio.open_positions} positions, the limit is {max_positions}")
        decision.narrative = f"NO BUY. The book is full at {max_positions} positions; close something before adding."
        return decision

    stock_cap = portfolio.capital * float(spec.get("max_portfolio_pct", 12.0)) / 100.0
    existing = portfolio.holdings.get(symbol, 0.0)
    headroom = max(0.0, stock_cap - existing)
    if target > headroom:
        target = headroom
        decision.binding_constraint = f"per-stock cap of {spec.get('max_portfolio_pct')}%"
        if existing > 0:
            decision.adjustments.append(f"already holding Rs {existing:,.0f} of {symbol}, so only the remainder is available")

    max_sector = float(constraints.get("max_sector_pct", 25.0))
    if sector:
        sector_cap = portfolio.capital * max_sector / 100.0
        sector_headroom = max(0.0, sector_cap - portfolio.sector_exposure.get(sector, 0.0))
        if target > sector_headroom:
            target = sector_headroom
            decision.binding_constraint = f"{max_sector:.0f}% sector cap on {sector}"
            decision.adjustments.append(
                f"{sector} is already {portfolio.sector_pct(sector):.1f}% of the book against a {max_sector:.0f}% cap"
            )

    max_heat = float(constraints.get("max_portfolio_heat_pct", 6.0))
    heat_headroom_pct = max(0.0, max_heat - portfolio.heat_pct)
    if risk_pct > heat_headroom_pct:
        if heat_headroom_pct <= 0:
            decision.rejections.append(
                f"portfolio heat is already {portfolio.heat_pct:.1f}% against a {max_heat:.0f}% ceiling"
            )
            decision.narrative = (
                f"NO BUY. Open risk across the book is already {portfolio.heat_pct:.1f}%, at the "
                f"{max_heat:.0f}% ceiling. Total exposure, not this stock, is the binding problem."
            )
            return decision
        scale = heat_headroom_pct / risk_pct
        target *= scale
        risk_pct = heat_headroom_pct
        decision.binding_constraint = f"{max_heat:.0f}% portfolio heat ceiling"
        decision.adjustments.append(
            f"scaled to {scale:.0%} so total open risk stays under the {max_heat:.0f}% ceiling"
        )

    if target > portfolio.cash:
        target = portfolio.cash
        decision.binding_constraint = "available cash"
        decision.adjustments.append("limited by uninvested cash")

    minimum = float(constraints.get("min_position_value", 5000.0))
    if target < minimum:
        decision.rejections.append(
            f"position of Rs {target:,.0f} is below the Rs {minimum:,.0f} minimum - brokerage would eat the edge"
        )
        decision.narrative = (
            f"NO BUY. Every constraint together leaves only Rs {target:,.0f}, which is too small "
            f"to be worth the costs."
        )
        return decision

    shares = int(target // price)
    if shares < 1:
        decision.rejections.append(f"Rs {target:,.0f} does not buy a single share at Rs {price:,.2f}")
        return decision

    decision.total_shares = shares
    decision.total_value = shares * price
    decision.pct_of_capital = decision.total_value / portfolio.capital * 100.0 if portfolio.capital else 0.0
    decision.recommendation = band.upper()  # type: ignore[assignment]

    if decision.stop_price is not None:
        decision.risk_amount = max(0.0, (price - decision.stop_price) * shares)
        decision.risk_pct_of_capital = (
            decision.risk_amount / portfolio.capital * 100.0 if portfolio.capital else 0.0
        )

    # --- Step 6: staged entry
    decision.tranches = build_tranches(decision.total_value, price, cfg)
    decision.narrative = describe(decision, price, sector)
    return decision


def build_tranches(total_value: float, price: float, cfg: Any) -> list[Tranche]:
    """Split the position into entry tranches.

    Dips deepen. Committing the whole position at the first signal is the
    single most common way this strategy loses money, so the default is to
    put half in now, add on further weakness while the thesis holds, and
    add the rest only once the trend actually turns.
    """
    staged = cfg.get("sizing.staged_entry", {}) or {}
    if not staged.get("enabled", True):
        shares = int(total_value // price)
        return [
            Tranche(1, 100.0, "signal", round(price, 2), shares * price, shares, "Full position at the signal price")
        ]

    tranches: list[Tranche] = []
    for i, spec in enumerate(staged.get("tranches", []) or [], start=1):
        pct = float(spec.get("pct", 0))
        trigger = str(spec.get("trigger", "signal"))
        value = total_value * pct / 100.0
        shares = int(value // price) if price > 0 else 0

        trigger_price: float | None
        if trigger == "signal":
            trigger_price = round(price, 2)
            description = f"Buy now at about Rs {price:,.2f}"
        elif trigger == "drop_pct":
            drop = float(spec.get("value", 7.0))
            trigger_price = round(price * (1 - drop / 100.0), 2)
            description = (
                f"Add if it falls a further {drop:.0f}% to about Rs {trigger_price:,.2f}, "
                f"and only while the thesis still holds"
            )
        elif trigger == "close_above_dma":
            dma = int(spec.get("value", 50))
            trigger_price = None
            description = f"Add on a close back above the {dma} DMA, confirming the trend has turned"
        else:
            trigger_price = None
            description = trigger

        tranches.append(Tranche(i, pct, trigger, trigger_price, round(value, 2), shares, description))

    return tranches


def describe(decision: SizingDecision, price: float, sector: str | None) -> str:
    """Plain-language summary of the decision and what bound it."""
    if not decision.is_buy:
        return decision.narrative or "NO BUY."

    first = decision.tranches[0] if decision.tranches else None
    edge = decision.edge

    lines = [
        f"{decision.recommendation} - Rs {decision.total_value:,.0f} total "
        f"({decision.total_shares:,} shares at about Rs {price:,.2f}), "
        f"{decision.pct_of_capital:.1f}% of capital."
    ]

    if first and len(decision.tranches) > 1:
        lines.append(
            f"Staged: Rs {first.value:,.0f} now, the rest across "
            f"{len(decision.tranches) - 1} further tranche(s)."
        )

    if decision.stop_price:
        lines.append(
            f"Stop at Rs {decision.stop_price:,.2f}, risking Rs {decision.risk_amount:,.0f} "
            f"({decision.risk_pct_of_capital:.2f}% of capital)."
        )

    if edge:
        provenance = {
            "ledger": f"measured over {edge.sample_size} closed trades",
            "blended": f"blended prior and {edge.sample_size} closed trades",
            "cold_start": "from the backtest, no live trades yet",
        }[edge.source]
        lines.append(
            f"Edge: {edge.win_probability:.0%} win rate at a {edge.payoff_ratio:.2f} payoff "
            f"({provenance}), giving raw Kelly of {decision.raw_kelly:.1%}, "
            f"applied at {decision.kelly_fraction_applied:g}x."
        )

    lines.append(f"Bound by the {decision.binding_constraint}.")
    return " ".join(lines)
