"""The exit doctrine: when to sell, decided before you ever need to.

The rules are written at entry and stored with the position, then re-checked
every day. That ordering is the whole point. Deciding to sell while watching
a position fall is the moment you are least able to think clearly, and a
doctrine written in advance is what removes that decision from the moment.

Rules are evaluated in severity order and the first urgent one wins. A thesis
break outranks a price target, because the reason to hold has gone regardless
of what the price happens to be doing.

Indian tax treatment is part of the doctrine rather than an afterthought.
Selling at eleven months costs 20% STCG against 12.5% LTCG, so an exit that
fires just short of the anniversary is reported with the rupee cost of
acting now against waiting.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any, Literal

log = logging.getLogger(__name__)

Action = Literal["HOLD", "TRIM", "EXIT", "REVIEW"]
Severity = Literal["info", "warn", "urgent"]

SEVERITY_ORDER = {"info": 0, "warn": 1, "urgent": 2}
ACTION_ORDER = {"HOLD": 0, "REVIEW": 1, "TRIM": 2, "EXIT": 3}


@dataclass
class ExitSignal:
    rule: str
    action: Action
    severity: Severity
    message: str
    detail: dict[str, Any] = field(default_factory=dict)
    trim_pct: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TaxLot:
    """One parcel of shares with its own acquisition date.

    Mirrors `src.portfolio.Lot` in the fields the exit rules need, kept as a
    plain structure here so this module stays free of database imports and
    remains unit-testable on its own.
    """

    quantity: float
    trade_date: date
    cost_per_share: float
    tranche_label: str | None = None

    def days_held(self, as_of: date | None = None) -> int:
        return ((as_of or date.today()) - self.trade_date).days

    def days_to_ltcg(self, ltcg_days: int = 366, as_of: date | None = None) -> int:
        return max(0, ltcg_days - self.days_held(as_of))

    def is_long_term(self, ltcg_days: int = 366, as_of: date | None = None) -> bool:
        return self.days_held(as_of) >= ltcg_days


@dataclass
class Position:
    """An open holding, as the exit engine needs to see it.

    `lots` is optional. Without it the position behaves as a single parcel
    bought on `entry_date`, which is what a legacy row looks like. With it,
    the tax rules work per lot - three tranches bought months apart reach
    long-term treatment on three different days, and FIFO decides which
    shares a sale actually disposes of.
    """

    symbol: str
    entry_date: date
    avg_entry_price: float
    quantity: float
    stop_price: float | None = None
    peak_price: float | None = None
    sector: str | None = None
    conviction: float | None = None
    trims_taken: list[float] = field(default_factory=list)   # gain levels already trimmed
    lots: list[TaxLot] = field(default_factory=list)

    @classmethod
    def from_state(
        cls,
        state: Any,
        *,
        stop_price: float | None = None,
        peak_price: float | None = None,
        sector: str | None = None,
        conviction: float | None = None,
    ) -> Position:
        """Build from a `src.portfolio.PositionState`."""
        return cls(
            symbol=state.symbol,
            entry_date=state.first_buy_date or date.today(),
            avg_entry_price=state.avg_cost,
            quantity=state.quantity,
            stop_price=stop_price,
            peak_price=peak_price,
            sector=sector,
            conviction=conviction,
            trims_taken=list(state.trims_taken),
            lots=[
                TaxLot(
                    quantity=lot.remaining,
                    trade_date=lot.trade_date,
                    cost_per_share=lot.cost_per_share,
                    tranche_label=lot.tranche_label,
                )
                for lot in state.lots
            ],
        )

    def effective_lots(self) -> list[TaxLot]:
        """The lots, or one synthetic lot standing in for the whole position."""
        if self.lots:
            return sorted(self.lots, key=lambda lot: lot.trade_date)
        return [TaxLot(self.quantity, self.entry_date, self.avg_entry_price)]

    def holding_days(self, as_of: date | None = None) -> int:
        """Age of the oldest shares still held.

        Deliberately the oldest rather than an average: under FIFO those are
        the shares a sale would dispose of first, so they are the ones whose
        holding period actually governs the tax on the next sale.
        """
        return ((as_of or date.today()) - self.entry_date).days

    def gain_pct(self, price: float) -> float:
        if self.avg_entry_price <= 0:
            return 0.0
        return (price - self.avg_entry_price) / self.avg_entry_price * 100.0

    def value(self, price: float) -> float:
        return price * self.quantity


@dataclass
class ThesisState:
    """The live facts the thesis-break rules test against.

    Every field defaults to the benign value, so a position is never exited
    because a data feed went quiet. `data_complete` records whether that
    silence was real - an unmonitored thesis is a risk worth surfacing, but
    it is not itself a reason to sell.
    """

    forensics_critical: bool = False
    promoter_pledge_pct: float | None = None
    promoter_pledge_prev_pct: float | None = None
    insider_net_sell_pct: float | None = None
    institutional_exit_quarters: int = 0
    auditor_resigned: bool = False
    sector_impaired: bool = False
    pe_vs_median_sd: float | None = None
    growth_decelerating: bool = False
    data_complete: bool = True
    missing: list[str] = field(default_factory=list)


@dataclass
class ExitDoctrine:
    """The written plan, issued with the buy and stored alongside it."""

    symbol: str
    entry_price: float
    stop_price: float | None
    targets: list[dict[str, Any]]
    trailing_stop: dict[str, Any]
    hard_stop_pct: float
    thesis_break_rules: list[str]
    tax_note: str
    issued_on: date

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["issued_on"] = self.issued_on.isoformat()
        return payload

    def describe(self) -> str:
        lines = [f"Exit doctrine for {self.symbol}, issued {self.issued_on.isoformat()}:"]
        for target in self.targets:
            lines.append(f"  - Trim {target['trim_pct']:.0f}% at +{target['gain_pct']:.0f}% (Rs {target['price']:,.2f})")
        lines.append(
            f"  - Once up {self.trailing_stop['activate_after_gain_pct']:.0f}%, trail "
            f"{self.trailing_stop['trail_pct']:.0f}% below the highest close since entry"
        )
        if self.stop_price:
            lines.append(f"  - Initial stop Rs {self.stop_price:,.2f}")
        lines.append(f"  - Stop-loss at {self.hard_stop_pct:.0f}%: sell")
        lines.append("  - Sell regardless of price if any of these break:")
        for rule in self.thesis_break_rules:
            lines.append(f"      * {rule}")
        lines.append(f"  - {self.tax_note}")
        return "\n".join(lines)


def build_doctrine(
    symbol: str,
    entry_price: float,
    cfg: Any,
    *,
    stop_price: float | None = None,
    entry_date: date | None = None,
) -> ExitDoctrine:
    """Write the exit plan at the moment of purchase."""
    targets = [
        {
            "gain_pct": float(t["gain_pct"]),
            "trim_pct": float(t["trim_pct"]),
            "price": round(entry_price * (1 + float(t["gain_pct"]) / 100.0), 2),
        }
        for t in (cfg.get("exit.staged_profit_taking", []) or [])
    ]

    tax = cfg.get("exit.tax", {}) or {}
    ltcg_days = int(tax.get("ltcg_days", 366))
    entry = entry_date or date.today()

    return ExitDoctrine(
        symbol=symbol,
        entry_price=entry_price,
        stop_price=stop_price,
        targets=targets,
        trailing_stop=dict(cfg.get("exit.trailing_stop", {}) or {}),
        hard_stop_pct=(
            -abs(float(cfg.get("sizing.stop_pct", 25.0)))
            if str(cfg.get("sizing.stop_rule", "atr")).lower() == "pct"
            else float(cfg.get("exit.hard_stop_pct", -25.0))
        ),
        thesis_break_rules=[
            "Financial forensics flag turns critical",
            "Promoter pledge rises sharply",
            "Promoters or insiders sell in size",
            f"Institutions reduce holdings for {int(cfg.get('exit.thesis_break.institutional_exit_quarters', 2))} consecutive quarters",
            "Auditor resigns or issues a qualified opinion",
            "The sector is structurally impaired, not merely out of favour",
        ],
        tax_note=(
            f"Long-term treatment at {tax.get('ltcg_rate_pct', 12.5)}% applies after {ltcg_days} days "
            f"(from {(entry + timedelta(days=ltcg_days)).isoformat()}); selling before that is taxed at "
            f"{tax.get('stcg_rate_pct', 20)}%."
        ),
        issued_on=entry,
    )


# --- Daily evaluation -------------------------------------------------------


def evaluate(
    position: Position,
    price: float,
    cfg: Any,
    *,
    thesis: ThesisState | None = None,
    as_of: date | None = None,
) -> list[ExitSignal]:
    """Check every rule against a position and return what fired.

    Returns all signals rather than only the winner, so the dashboard can
    show the complete picture - "approaching the first target, and 40 days
    from long-term tax treatment" is more useful than either fact alone.
    Use `decide()` to collapse them into one action.
    """
    today = as_of or date.today()
    thesis = thesis or ThesisState()
    signals: list[ExitSignal] = []

    gain = position.gain_pct(price)
    days_held = position.holding_days(today)

    signals.extend(_thesis_break_signals(position, thesis, cfg, gain))
    signals.extend(_target_signals(position, price, gain, cfg))
    signals.extend(_trailing_stop_signals(position, price, gain, cfg))
    signals.extend(_stop_signals(position, price, gain, cfg))
    signals.extend(_tax_signals(position, price, gain, days_held, cfg))
    signals.extend(_valuation_signals(position, thesis, gain, cfg))

    if not thesis.data_complete and thesis.missing:
        signals.append(
            ExitSignal(
                rule="data_gap",
                action="REVIEW",
                severity="warn",
                message=(
                    f"Thesis could not be fully checked: {', '.join(thesis.missing)}. "
                    f"This is not a reason to sell, but the position is running unmonitored."
                ),
                detail={"missing": thesis.missing},
            )
        )

    if not signals:
        signals.append(
            ExitSignal(
                rule="hold",
                action="HOLD",
                severity="info",
                message=f"Thesis intact, {gain:+.1f}% after {days_held} days. Nothing to do.",
                detail={"gain_pct": round(gain, 2), "days_held": days_held},
            )
        )

    return signals


def _thesis_break_signals(
    position: Position, thesis: ThesisState, cfg: Any, gain: float
) -> list[ExitSignal]:
    """Reasons to sell regardless of price."""
    signals: list[ExitSignal] = []

    if thesis.forensics_critical:
        signals.append(
            ExitSignal(
                "thesis_break", "EXIT", "urgent",
                "Forensics flag turned critical. The reason to own this has gone - exit regardless of price.",
                {"trigger": "forensics", "gain_pct": round(gain, 2)},
            )
        )

    if thesis.promoter_pledge_pct is not None:
        cap = float(cfg.get("quality_gate.max_promoter_pledge_pct", 20.0))
        previous = thesis.promoter_pledge_prev_pct
        if thesis.promoter_pledge_pct > cap:
            signals.append(
                ExitSignal(
                    "thesis_break", "EXIT", "urgent",
                    f"Promoter pledge at {thesis.promoter_pledge_pct:.1f}% has passed the {cap:.0f}% limit.",
                    {"trigger": "pledge", "pledge_pct": thesis.promoter_pledge_pct},
                )
            )
        elif previous is not None and thesis.promoter_pledge_pct - previous >= 5.0:
            signals.append(
                ExitSignal(
                    "thesis_break", "REVIEW", "warn",
                    f"Promoter pledge jumped from {previous:.1f}% to {thesis.promoter_pledge_pct:.1f}%. "
                    f"Still inside the limit, but the direction matters.",
                    {"trigger": "pledge_rising"},
                )
            )

    if thesis.insider_net_sell_pct is not None:
        alarm = float(cfg.get("ownership.insider_net_sell_alarm_pct", 1.0))
        if thesis.insider_net_sell_pct >= alarm:
            signals.append(
                ExitSignal(
                    "thesis_break", "EXIT", "urgent",
                    f"Insiders have net sold {thesis.insider_net_sell_pct:.2f}% of the company. "
                    f"The people with the best information are leaving.",
                    {"trigger": "insider_selling"},
                )
            )

    required = int(cfg.get("exit.thesis_break.institutional_exit_quarters", 2))
    if thesis.institutional_exit_quarters >= required:
        signals.append(
            ExitSignal(
                "thesis_break", "EXIT", "urgent",
                f"Institutions have cut their holding for {thesis.institutional_exit_quarters} consecutive quarters.",
                {"trigger": "institutional_exit"},
            )
        )

    if thesis.auditor_resigned:
        signals.append(
            ExitSignal(
                "thesis_break", "EXIT", "urgent",
                "The auditor has resigned or qualified its opinion. Treat the reported numbers as unreliable.",
                {"trigger": "auditor"},
            )
        )

    if thesis.sector_impaired:
        signals.append(
            ExitSignal(
                "thesis_break", "REVIEW", "warn",
                "The sector looks structurally impaired rather than merely out of favour. Re-examine the thesis.",
                {"trigger": "sector"},
            )
        )

    return signals


def _target_signals(position: Position, price: float, gain: float, cfg: Any) -> list[ExitSignal]:
    signals: list[ExitSignal] = []
    for target in cfg.get("exit.staged_profit_taking", []) or []:
        level = float(target["gain_pct"])
        trim = float(target["trim_pct"])
        if gain >= level and level not in position.trims_taken:
            signals.append(
                ExitSignal(
                    "target", "TRIM", "info",
                    f"Up {gain:.1f}%, past the +{level:.0f}% target. Trim {trim:.0f}% and let the rest run.",
                    {"gain_pct": round(gain, 2), "target_pct": level, "price": price},
                    trim_pct=trim,
                )
            )
    return signals


def _trailing_stop_signals(position: Position, price: float, gain: float, cfg: Any) -> list[ExitSignal]:
    spec = cfg.get("exit.trailing_stop", {}) or {}
    activate = float(spec.get("activate_after_gain_pct", 30.0))
    trail = float(spec.get("trail_pct", 20.0))

    peak = position.peak_price
    if peak is None or peak <= 0:
        return []

    peak_gain = position.gain_pct(peak)
    if peak_gain < activate:
        return []

    trigger = peak * (1 - trail / 100.0)
    if price <= trigger:
        return [
            ExitSignal(
                "trailing_stop", "EXIT", "urgent",
                f"Down {trail:.0f}% from the peak of Rs {peak:,.2f}. "
                f"Trailing stop hit at Rs {price:,.2f}, locking in {gain:+.1f}%.",
                {"peak": peak, "trigger": round(trigger, 2), "gain_pct": round(gain, 2)},
            )
        ]

    distance = (price - trigger) / price * 100.0
    if distance <= 5.0:
        return [
            ExitSignal(
                "trailing_stop", "HOLD", "info",
                f"Trailing stop is {distance:.1f}% away, at Rs {trigger:,.2f}.",
                {"trigger": round(trigger, 2), "distance_pct": round(distance, 2)},
            )
        ]
    return []


def _stop_signals(position: Position, price: float, gain: float, cfg: Any) -> list[ExitSignal]:
    signals: list[ExitSignal] = []

    hard = float(cfg.get("exit.hard_stop_pct", -25.0))
    if str(cfg.get("sizing.stop_rule", "atr")).lower() == "pct":
        hard = -abs(float(cfg.get("sizing.stop_pct", abs(hard))))
        if gain <= hard:
            signals.append(
                ExitSignal(
                    "hard_stop", "EXIT", "urgent",
                    f"Down {gain:.1f}%, through the {hard:.0f}% stop-loss. Sell. This is the "
                    f"stop that was backtested: selling here beat a tighter ATR stop on both "
                    f"growth and drawdown from 2022 on.",
                    {"gain_pct": round(gain, 2), "hard_stop_pct": hard},
                )
            )
        return signals

    if gain <= hard:
        signals.append(
            ExitSignal(
                "hard_stop", "REVIEW", "urgent",
                f"Down {gain:.1f}%, past the {hard:.0f}% hard stop. This forces a committee re-review, "
                f"not an automatic sale: if the thesis is intact the fall may be the opportunity, and "
                f"if it is not, this should already have exited on a thesis break.",
                {"gain_pct": round(gain, 2), "hard_stop_pct": hard},
            )
        )

    if position.stop_price and price <= position.stop_price and gain > hard:
        signals.append(
            ExitSignal(
                "initial_stop", "REVIEW", "warn",
                f"Price Rs {price:,.2f} has reached the initial stop of Rs {position.stop_price:,.2f}.",
                {"stop_price": position.stop_price, "gain_pct": round(gain, 2)},
            )
        )

    return signals


def _tax_signals(
    position: Position, price: float, gain: float, days_held: int, cfg: Any
) -> list[ExitSignal]:
    """Flag the short-term boundary, lot by lot.

    A staged entry buys the same position three times, months apart, so there
    is no single anniversary. Each lot has its own, and under FIFO a sale
    disposes of the oldest shares first - which means the lot worth warning
    about is the oldest one still short-term, not the position as a whole.

    The rupee figures are computed on that lot's shares alone. Quoting the
    whole position's profit would overstate what is actually at stake in the
    next few weeks.
    """
    tax = cfg.get("exit.tax", {}) or {}
    ltcg_days = int(tax.get("ltcg_days", 366))
    warn_window = int(tax.get("warn_days_before_ltcg", 45))
    stcg_rate = float(tax.get("stcg_rate_pct", 20.0)) / 100.0
    ltcg_rate = float(tax.get("ltcg_rate_pct", 12.5)) / 100.0
    exemption = float(tax.get("ltcg_exemption", 125000.0))

    lots = position.effective_lots()
    short_term = [lot for lot in lots if not lot.is_long_term(ltcg_days)]

    if not short_term:
        return []

    # FIFO sells the oldest first, so the oldest short-term lot is the one
    # whose clock governs the next sale.
    lot = min(short_term, key=lambda l: l.trade_date)
    days_to_go = lot.days_to_ltcg(ltcg_days)

    if days_to_go > warn_window:
        return []

    profit = (price - lot.cost_per_share) * lot.quantity
    if profit <= 0:
        return []

    stcg_due = profit * stcg_rate
    ltcg_due = max(0.0, profit - exemption) * ltcg_rate
    saving = stcg_due - ltcg_due

    long_term_qty = sum(l.quantity for l in lots if l.is_long_term(ltcg_days))
    position_note = ""
    if len(lots) > 1:
        label = f" ({lot.tranche_label})" if lot.tranche_label else ""
        position_note = (
            f" This covers the {lot.quantity:g} shares bought on "
            f"{lot.trade_date.isoformat()}{label}, not the whole holding"
        )
        if long_term_qty > 0:
            position_note += f"; {long_term_qty:g} shares are already long-term"
        position_note += "."

    return [
        ExitSignal(
            "ltcg_deadline", "HOLD", "warn",
            f"{days_to_go} days from long-term tax treatment on the oldest shares. On their "
            f"unrealised profit of Rs {profit:,.0f}, selling now costs Rs {stcg_due:,.0f} in tax "
            f"against Rs {ltcg_due:,.0f} after the anniversary - a difference of "
            f"Rs {saving:,.0f}. Worth waiting unless the thesis has broken.{position_note}",
            {
                "days_to_ltcg": days_to_go,
                "lot_quantity": lot.quantity,
                "lot_date": lot.trade_date.isoformat(),
                "lot_label": lot.tranche_label,
                "long_term_quantity": long_term_qty,
                "unrealised_profit": round(profit, 2),
                "stcg_due": round(stcg_due, 2),
                "ltcg_due": round(ltcg_due, 2),
                "saving": round(saving, 2),
            },
        )
    ]


def _valuation_signals(position: Position, thesis: ThesisState, gain: float, cfg: Any) -> list[ExitSignal]:
    spec = cfg.get("exit.valuation_exit", {}) or {}
    threshold = float(spec.get("pe_vs_5y_median_sd", 1.0))

    if thesis.pe_vs_median_sd is None or thesis.pe_vs_median_sd < threshold:
        return []

    if spec.get("require_growth_deceleration", True) and not thesis.growth_decelerating:
        return [
            ExitSignal(
                "valuation", "HOLD", "info",
                f"Valuation is {thesis.pe_vs_median_sd:.1f} standard deviations above its 5-year median, "
                f"but growth is still intact. Expensive is not the same as finished.",
                {"pe_sd": thesis.pe_vs_median_sd},
            )
        ]

    return [
        ExitSignal(
            "valuation", "TRIM", "warn",
            f"Valuation {thesis.pe_vs_median_sd:.1f} SD above its 5-year median while growth decelerates. "
            f"The re-rating has run ahead of the business.",
            {"pe_sd": thesis.pe_vs_median_sd},
            trim_pct=50.0,
        )
    ]


def decide(signals: list[ExitSignal]) -> ExitSignal:
    """Collapse the day's signals into the one action to take.

    Ordered by action first, then severity. A thesis-break EXIT always
    outranks a tax warning, which is the correct priority: tax is a cost,
    a broken thesis is a loss.
    """
    if not signals:
        return ExitSignal("hold", "HOLD", "info", "No signals.")
    return max(signals, key=lambda s: (ACTION_ORDER[s.action], SEVERITY_ORDER[s.severity]))


def update_peak(position: Position, price: float, as_of: date | None = None) -> bool:
    """Track the highest price seen while held.

    Feeds the trailing stop, and afterwards the post-mortem's "how much more
    could we have made" question. Maximum favourable excursion cannot be
    reconstructed later, so it has to be recorded as it happens.
    """
    if position.peak_price is None or price > position.peak_price:
        position.peak_price = price
        return True
    return False
