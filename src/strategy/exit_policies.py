"""How open positions are closed, for both strategies.

Each policy answers three questions for the simulator: where the resting
stop and target sit once filled (`on_open`), whether either was touched
during a session (`intraday`), and what the close says - a rule exit, a time
stop, a trim, a scale-in (`at_close`).

The live system and the research backtest share these, so the stop and
target an alert quotes are the ones that were tested.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from src.strategy.simulator import Bars, Order, Trade
from src.strategy.swing import ExitPlan, SwingRule


# --- Swing --------------------------------------------------------------------------


class SwingPolicy:
    """A swing trade under one rule and one stop/target plan.

    With a fixed ATR target the rule's own exit is switched off - the target
    replaces it - so each of the nine plans is a genuinely different trade.
    The time stop always applies.
    """

    def __init__(self, rule: SwingRule, plan: ExitPlan, own_exit: Any):
        self.rule = rule
        self.plan = plan
        self.own_exit = own_exit          # bool array aligned with the stock's bars

    def on_open(self, trade: Trade, bars: Bars, i: int) -> None:
        trade.stop, trade.target = self.plan.levels(trade.first_price, trade.atr)

    def intraday(self, trade: Trade, bars: Bars, i: int) -> list[Order]:
        if trade.stop is not None and bars.low[i] <= trade.stop:
            return [Order("SELL", "stop", price=trade.stop, kind="stop")]
        if trade.target is not None and bars.high[i] >= trade.target:
            return [Order("SELL", "target", price=trade.target, kind="target")]
        return []

    def at_close(self, trade: Trade, bars: Bars, i: int) -> list[Order]:
        if self.plan.target_atr is None and bool(self.own_exit[i]):
            return [Order("SELL", "rule_exit")]
        if trade.sessions >= self.plan.max_hold:
            return [Order("SELL", "time_stop")]

        # TPS: add the next tranche on each close below the last purchase.
        tranches = self.rule.scale_in
        if trade.tranches_done < len(tranches):
            last_buy = [f for f in trade.fills if f.side == "BUY"][-1].price
            if bars.close[i] < last_buy:
                return [Order("BUY", f"scale_in_{trade.tranches_done + 1}",
                              fraction=tranches[trade.tranches_done])]
        return []


# --- Long-term ----------------------------------------------------------------------


@dataclass(frozen=True)
class LongTermPlan:
    """One way of exiting a long-term dip position.

    L1 - what the alerts show today: a 2.5xATR stop, a quarter sold at +25%
         and another at +50%, then a 20% trailing stop once up 30%.
    L2 - the same trims, with a -25% stop instead of the ATR stop.
    L3 - no fixed targets: a chandelier stop, highest high minus 3xATR(22),
         which only ever rises (Chuck LeBeau's exit).
    """

    key: str
    label: str
    stop_atr: float | None = None
    stop_pct: float | None = None
    trims: tuple[tuple[float, float], ...] = ()      # (gain %, fraction of original)
    trail_after_pct: float | None = None
    trail_pct: float | None = None
    chandelier_atr: float | None = None
    max_hold: int = 500                              # sessions, about two years


def long_term_plans(cfg: Any) -> dict[str, LongTermPlan]:
    trims = tuple(
        (float(t["gain_pct"]), float(t["trim_pct"]) / 100.0)
        for t in (cfg.get("exit.staged_profit_taking", []) or [])
    )
    trailing = cfg.get("exit.trailing_stop", {}) or {}
    trail_after = float(trailing.get("activate_after_gain_pct", 30.0))
    trail = float(trailing.get("trail_pct", 20.0))
    return {
        "L1": LongTermPlan(
            "L1", "2.5xATR stop, trims at +25%/+50%, 20% trail after +30% (current alerts)",
            stop_atr=float(cfg.get("sizing.atr_stop_multiplier", 2.5)),
            trims=trims, trail_after_pct=trail_after, trail_pct=trail,
        ),
        "L2": LongTermPlan(
            "L2", "-25% stop, trims at +25%/+50%, 20% trail after +30%",
            stop_pct=abs(float(cfg.get("exit.hard_stop_pct", -25.0))),
            trims=trims, trail_after_pct=trail_after, trail_pct=trail,
        ),
        "L3": LongTermPlan(
            "L3", "chandelier stop: highest high - 3xATR(22), no fixed targets",
            chandelier_atr=3.0,
        ),
    }


def _width(bars: Bars, i: int, fallback: float) -> float:
    """ATR(22) on this bar, or the entry ATR when it is not yet defined.

    A NaN here would make the stop NaN, and every comparison with NaN is
    false - the stop would silently never fire.
    """
    atr22 = bars.extra.get("atr22")
    value = float(atr22[i]) if atr22 is not None else float("nan")
    if math.isnan(value) or value <= 0:
        value = fallback
    return value if not math.isnan(value) else 0.0


class LongTermPolicy:
    def __init__(self, plan: LongTermPlan):
        self.plan = plan

    def on_open(self, trade: Trade, bars: Bars, i: int) -> None:
        entry = trade.first_price
        if self.plan.stop_atr and trade.atr > 0:
            trade.stop = round(entry - self.plan.stop_atr * trade.atr, 2)
        elif self.plan.stop_pct:
            trade.stop = round(entry * (1 - self.plan.stop_pct / 100.0), 2)
        if self.plan.chandelier_atr:
            width = _width(bars, i, trade.atr)
            if width > 0:
                trade.stop = round(bars.high[i] - self.plan.chandelier_atr * width, 2)
        trade.state["original_qty"] = trade.quantity
        trade.state["trims_done"] = 0
        if self.plan.trims:
            trade.target = round(entry * (1 + self.plan.trims[0][0] / 100.0), 2)

    def intraday(self, trade: Trade, bars: Bars, i: int) -> list[Order]:
        if trade.stop is not None and bars.low[i] <= trade.stop:
            return [Order("SELL", "stop", price=trade.stop, kind="stop")]

        done = trade.state.get("trims_done", 0)
        if done < len(self.plan.trims):
            gain, fraction = self.plan.trims[done]
            level = trade.first_price * (1 + gain / 100.0)
            if bars.high[i] >= level:
                qty = int(trade.state["original_qty"] * fraction)
                trade.state["trims_done"] = done + 1
                nxt = done + 1
                trade.target = (
                    round(trade.first_price * (1 + self.plan.trims[nxt][0] / 100.0), 2)
                    if nxt < len(self.plan.trims) else None
                )
                if qty >= 1 and qty < trade.quantity:
                    return [Order("SELL", f"trim_{gain:g}pct", quantity=qty,
                                  price=level, kind="target")]
        return []

    def at_close(self, trade: Trade, bars: Bars, i: int) -> list[Order]:
        entry = trade.first_price
        if self.plan.trail_after_pct is not None and self.plan.trail_pct is not None:
            peak_gain = (trade.peak_close - entry) / entry * 100.0
            if peak_gain >= self.plan.trail_after_pct and \
                    bars.close[i] <= trade.peak_close * (1 - self.plan.trail_pct / 100.0):
                return [Order("SELL", "trailing_stop")]

        if self.plan.chandelier_atr:
            width = _width(bars, i, trade.atr)
            if width > 0:
                level = round(trade.peak - self.plan.chandelier_atr * width, 2)
                # A chandelier stop only ever rises.
                if trade.stop is None or level > trade.stop:
                    trade.stop = level

        if trade.sessions >= self.plan.max_hold:
            return [Order("SELL", "max_hold")]
        return []
