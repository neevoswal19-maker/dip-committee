"""The intraday rules: opening-range breakout, then a pullback to VWAP / 20 EMA.

As specified by the owner on 2026-10-17 and fixed before any test:

1. Opening range: the high and low of the 9:15, 9:20 and 9:25 bars. The day
   is skipped for a symbol whose range is wider than its limit (0.85% for
   NIFTYBEES, 1.10% for BANKBEES).
2. Arm (bars starting 9:30-10:55): a close beyond the range with the close
   beyond both VWAP and the 20 EMA, volume above its 10-bar average, and the
   20 EMA moving the trade's way and on the trade's side of VWAP.
3. Pullback (candles 1-6 after the breakout, bars starting before 13:30): a
   low (high, for shorts) within 0.15% of the 20 EMA or VWAP, every pullback
   bar on less volume than the breakout bar and RSI(14) on the trade's side of
   50. Entry on the close of the first candle from the 2nd to the 6th that
   closes the trade's way and back beyond both lines. Otherwise it expires
   and a new breakout may arm, until one trade has been taken that day.
4. Stop: beyond the pullback's extreme or 0.10% beyond VWAP, whichever is
   further. At intraday.partial_r (2R) half is sold and the stop moves to entry. The rest leaves
   on a 5-minute close back through the 20 EMA (at that close), at intraday.final_r (2.5R), or at
   15:10 (the close of the 15:05 bar).

Every decision reads only completed bars, so the live job, which calls this on
the day's bars so far, gets exactly the events a backtest would.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date, time
from typing import Any

import numpy as np
import pandas as pd

from src import indicators as ind

#: Judging the paper trades, fixed before the first one.
PASS = {"min_trades": 40, "min_profit_factor": 1.3, "max_drawdown_pct": 10.0}

ORB_BARS = (time(9, 15), time(9, 20), time(9, 25))
ARM_FROM, ARM_UNTIL = time(9, 30), time(11, 0)       # bar start times, half-open
ENTRY_UNTIL = time(13, 30)
SQUARE_OFF_BAR = time(15, 5)                          # closes at 15:10
BAR_MINUTES = 5


@dataclass
class Event:
    when: pd.Timestamp            # when the bar closed: the moment the event is known
    symbol: str
    kind: str                     # skipped | armed | expired | cancelled | entry | partial | exit
    side: str | None = None       # LONG | SHORT
    price: float | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def key(self) -> str:
        return f"{self.symbol}|{self.when:%Y-%m-%d %H:%M}|{self.kind}"

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["when"] = self.when.isoformat()
        return out


@dataclass
class DayTrade:
    """The one trade a symbol took on a day, with its fills."""

    day: date
    symbol: str
    side: str
    entry_at: pd.Timestamp
    entry: float
    stop: float
    risk_per_unit: float
    units: int
    fills: list[tuple[pd.Timestamp, str, int, float]] = field(default_factory=list)  # when, reason, units, price
    exit_reason: str | None = None

    @property
    def open_units(self) -> int:
        return self.units - sum(u for _, _, u, _ in self.fills)

    @property
    def closed(self) -> bool:
        return self.open_units == 0


# --- Indicators -------------------------------------------------------------------------


def with_indicators(bars: pd.DataFrame) -> pd.DataFrame:
    """VWAP reset each session; 20 EMA, RSI(14) and 10-bar volume average run
    continuously across sessions, so the morning starts warmed up."""
    f = bars.sort_index().copy()
    for column in ("open", "high", "low", "close", "volume"):
        f[column] = pd.to_numeric(f[column], errors="coerce")
    session = f.index.normalize()
    typical = (f["high"] + f["low"] + f["close"]) / 3.0
    pv = (typical * f["volume"]).groupby(session).cumsum()
    vol = f["volume"].groupby(session).cumsum()
    f["vwap"] = pv / vol.replace(0.0, np.nan)
    f["ema20"] = f["close"].ewm(span=20, adjust=False).mean()
    f["rsi"] = ind.rsi(f["close"], 14)
    f["vol_sma10"] = f["volume"].rolling(10, min_periods=10).mean()
    return f


# --- The rules --------------------------------------------------------------------------


def _bar_close(start: pd.Timestamp) -> pd.Timestamp:
    return start + pd.Timedelta(minutes=BAR_MINUTES)


def session_events(f: pd.DataFrame, day: date, symbol: str, params: dict[str, Any]) -> tuple[list[Event], DayTrade | None]:
    """Every event for one symbol on one day, from completed bars only.

    `f` comes from with_indicators and may hold earlier sessions (for the
    warm-up) but must not hold bars that had not closed at the time of use.
    """
    events: list[Event] = []
    today = f[f.index.normalize() == pd.Timestamp(day)]
    if today.empty:
        return events, None
    times = [ts.time() for ts in today.index]

    orb = today[[t in ORB_BARS for t in times]]
    if len(orb) < len(ORB_BARS):
        return events, None
    orb_high, orb_low = float(orb["high"].max()), float(orb["low"].min())
    width = (orb_high - orb_low) / orb_low * 100.0
    if width > float(params["max_range_pct"]):
        events.append(Event(_bar_close(orb.index[-1]), symbol, "skipped",
                            detail={"range_pct": round(width, 2), "limit_pct": params["max_range_pct"]}))
        return events, None

    rows = list(today.itertuples())
    armed: dict[str, Any] | None = None
    trade: DayTrade | None = None
    prev_ema = None
    risk_rs = float(params["risk_per_trade"])
    max_exposure = float(params["max_exposure"])
    slip = float(params.get("slippage_pct", 0.02)) / 100.0
    partial_r = float(params.get("partial_r", 1.5))
    final_r = params.get("final_r", 3.0)
    final_r = float(final_r) if final_r else None     # None: the runner only trails

    for n, row in enumerate(rows):
        start = row.Index
        t = start.time()
        closed_at = _bar_close(start)
        ema, vwap, rsi = float(row.ema20), float(row.vwap), float(row.rsi)
        ema_rising = prev_ema is not None and ema > prev_ema
        ema_falling = prev_ema is not None and ema < prev_ema
        prev_ema = ema

        # --- Managing the open trade
        if trade is not None and not trade.closed:
            long = trade.side == "LONG"
            sign = 1 if long else -1
            r = trade.risk_per_unit
            partial_done = any(reason.startswith("partial_") for _, reason, _, _ in trade.fills)
            stop_level = trade.entry if partial_done else trade.stop
            hit_stop = row.low <= stop_level if long else row.high >= stop_level
            if hit_stop:
                fill = min(row.open, stop_level) if long else max(row.open, stop_level)
                reason = "breakeven" if partial_done else "stop"
                _exit(trade, events, closed_at, reason, trade.open_units, fill * (1 - sign * slip))
                continue
            target1 = trade.entry + sign * partial_r * r
            target3 = trade.entry + sign * final_r * r if final_r else None
            if not partial_done and (row.high >= target1 if long else row.low <= target1):
                half = trade.units // 2
                if half >= 1:
                    trade.fills.append((closed_at, f"partial_{partial_r:g}R", half, target1))
                    events.append(Event(closed_at, symbol, "partial", trade.side, round(target1, 2),
                                        {"units": half, "new_stop": round(trade.entry, 2)}))
                partial_done = True
            if partial_done and target3 is not None and (row.high >= target3 if long else row.low <= target3):
                _exit(trade, events, closed_at, f"target_{final_r:g}R", trade.open_units, target3)
                continue
            if partial_done and (row.close < ema if long else row.close > ema):
                _exit(trade, events, closed_at, "trail_ema20", trade.open_units, row.close * (1 - sign * slip))
                continue
            if t >= SQUARE_OFF_BAR:
                _exit(trade, events, closed_at, "time_stop_15:10", trade.open_units, row.close * (1 - sign * slip))
            continue

        if trade is not None:            # one trade a day
            continue

        # --- A setup waiting for its pullback
        if armed is not None:
            armed["k"] += 1
            k = armed["k"]
            long = armed["side"] == "LONG"
            near = (row.low <= ema * 1.0015 or row.low <= vwap * 1.0015) if long else \
                   (row.high >= ema * 0.9985 or row.high >= vwap * 0.9985)
            armed["touched"] = armed["touched"] or near
            armed["extreme"] = min(armed["extreme"], row.low) if long else max(armed["extreme"], row.high)
            rsi_ok = (rsi >= 50) if long else (rsi <= 50)
            trigger = (k >= 2 and armed["touched"] and t < ENTRY_UNTIL
                       and (row.close > row.open and row.close > ema and row.close > vwap if long
                            else row.close < row.open and row.close < ema and row.close < vwap))
            if not rsi_ok:
                events.append(Event(closed_at, symbol, "cancelled", armed["side"],
                                    detail={"reason": f"RSI {rsi:.0f} crossed 50"}))
                armed = None
            elif trigger:
                entry = row.close * (1 + slip) if long else row.close * (1 - slip)
                stop = min(armed["extreme"], vwap * 0.999) if long else max(armed["extreme"], vwap * 1.001)
                risk = abs(entry - stop)
                units = int(risk_rs // risk) if risk > 0 else 0
                if units * entry > max_exposure:
                    units = int(max_exposure // entry)
                if units >= 2:
                    trade = DayTrade(day, symbol, armed["side"], closed_at, entry, stop, risk, units)
                    sign = 1 if long else -1
                    events.append(Event(closed_at, symbol, "entry", armed["side"], round(entry, 2), {
                        "stop": round(stop, 2), "partial_r": partial_r, "final_r": final_r,
                        "target_partial": round(entry + sign * partial_r * risk, 2),
                        "target_final": round(entry + sign * final_r * risk, 2) if final_r else None,
                        "units": units,
                        "exposure": round(units * entry), "margin": round(units * entry / float(params["leverage"])),
                        "risk_rs": round(units * risk), "rsi": round(rsi, 1),
                    }))
                armed = None
            elif row.volume >= armed["breakout_volume"]:
                events.append(Event(closed_at, symbol, "cancelled", armed["side"],
                                    detail={"reason": "pullback volume above the breakout bar"}))
                armed = None
            elif k >= 6:
                events.append(Event(closed_at, symbol, "expired", armed["side"],
                                    detail={"reason": "no trigger within 6 candles"}))
                armed = None
            continue

        # --- Looking for a breakout to arm
        if not (ARM_FROM <= t < ARM_UNTIL) or math.isnan(row.vol_sma10) or math.isnan(vwap):
            continue
        volume_ok = row.volume > row.vol_sma10
        if (row.close > orb_high and row.close > vwap and row.close > ema and volume_ok
                and ema_rising and ema >= vwap):
            side = "LONG"
        elif (row.close < orb_low and row.close < vwap and row.close < ema and volume_ok
              and ema_falling and ema <= vwap):
            side = "SHORT"
        else:
            continue
        # The stop sits beyond the pullback's own extreme, so it starts empty.
        armed = {"side": side, "k": 0, "touched": False, "breakout_volume": row.volume,
                 "extreme": math.inf if side == "LONG" else -math.inf}
        events.append(Event(closed_at, symbol, "armed", side, round(row.close, 2), {
            "orb_high": round(orb_high, 2), "orb_low": round(orb_low, 2),
            "volume_x_avg": round(row.volume / row.vol_sma10, 2)}))

    return events, trade


def _exit(trade: DayTrade, events: list[Event], when, reason: str, units: int, price: float) -> None:
    trade.fills.append((when, reason, units, price))
    trade.exit_reason = reason
    events.append(Event(when, trade.symbol, "exit", trade.side, round(price, 2),
                        {"reason": reason, "units": units}))


# --- Costs and results ------------------------------------------------------------------


def leg_cost(side: str, units: int, price: float, costs: dict[str, Any]) -> float:
    """Groww intraday charges for one order, in rupees."""
    value = units * price
    if value <= 0:
        return 0.0
    brokerage = min(float(costs.get("brokerage_max", 20.0)), value * float(costs.get("brokerage_pct", 0.1)) / 100.0)
    exchange = value * float(costs.get("exchange_pct", 0.00297)) / 100.0
    sebi = value * float(costs.get("sebi_pct", 0.0001)) / 100.0
    gst = (brokerage + exchange + sebi) * float(costs.get("gst_pct", 18.0)) / 100.0
    stt = value * float(costs.get("stt_sell_pct", 0.025)) / 100.0 if side == "SELL" else 0.0
    stamp = value * float(costs.get("stamp_buy_pct", 0.003)) / 100.0 if side == "BUY" else 0.0
    return brokerage + exchange + sebi + gst + stt + stamp


def result(trade: DayTrade, costs: dict[str, Any]) -> dict[str, Any]:
    """Net P&L and R of a closed (or still open) trade."""
    long = trade.side == "LONG"
    opening = "BUY" if long else "SELL"
    closing = "SELL" if long else "BUY"
    gross = 0.0
    charges = leg_cost(opening, trade.units, trade.entry, costs)
    for _, _, units, price in trade.fills:
        gross += units * (price - trade.entry) * (1 if long else -1)
        charges += leg_cost(closing, units, price, costs)
    net = gross - charges
    risk = trade.units * trade.risk_per_unit
    return {
        "day": trade.day.isoformat(), "symbol": trade.symbol, "side": trade.side,
        "entry_at": trade.entry_at.isoformat(), "entry": round(trade.entry, 2), "stop": round(trade.stop, 2),
        "units": trade.units, "exit_reason": trade.exit_reason, "closed": trade.closed,
        "gross": round(gross, 2), "charges": round(charges, 2), "net": round(net, 2),
        "r": round(net / risk, 3) if risk > 0 else 0.0,
    }


def scorecard(results: list[dict[str, Any]], capital: float) -> dict[str, Any]:
    closed = [r for r in results if r.get("closed")]
    if not closed:
        return {"trades": 0}
    nets = [r["net"] for r in closed]
    gains = sum(x for x in nets if x > 0)
    losses = -sum(x for x in nets if x < 0)
    equity = np.cumsum(nets) + capital
    peak = np.maximum.accumulate(np.concatenate([[capital], equity]))[1:]
    drawdown = float(((equity - peak) / peak).min() * 100.0)
    return {
        "trades": len(closed),
        "win_rate": sum(1 for x in nets if x > 0) / len(closed),
        "avg_r": float(np.mean([r["r"] for r in closed])),
        "net": float(sum(nets)),
        "avg_net": float(np.mean(nets)),
        "profit_factor": gains / losses if losses > 0 else float("inf"),
        "max_drawdown_pct": drawdown,
        "longs": sum(1 for r in closed if r["side"] == "LONG"),
        "shorts": sum(1 for r in closed if r["side"] == "SHORT"),
    }


def judge(card: dict[str, Any]) -> tuple[bool | None, list[str]]:
    """None until 40 trades; then pass or fail against PASS."""
    n = card.get("trades", 0)
    if n < PASS["min_trades"]:
        return None, [f"{n} of {PASS['min_trades']} trades so far"]
    reasons = []
    if card["profit_factor"] < PASS["min_profit_factor"]:
        reasons.append(f"profit factor {card['profit_factor']:.2f} below {PASS['min_profit_factor']}")
    if card["avg_net"] <= 0:
        reasons.append(f"average trade Rs {card['avg_net']:,.0f} after costs")
    if card["max_drawdown_pct"] < -PASS["max_drawdown_pct"]:
        reasons.append(f"worst drawdown {card['max_drawdown_pct']:.1f}% deeper than -{PASS['max_drawdown_pct']:.0f}%")
    return not reasons, reasons


def params_for(symbol: str, cfg: Any) -> dict[str, Any]:
    ic = cfg.get("intraday", {}) or {}
    margin = float(ic.get("max_margin", 150000))
    leverage = float(ic.get("leverage", 5))
    return {
        "max_range_pct": float((ic.get("symbols", {}) or {}).get(symbol, {}).get("max_range_pct", 0.85)),
        "risk_per_trade": float(ic.get("risk_per_trade", 5000)),
        "max_exposure": margin * leverage,
        "leverage": leverage,
        "slippage_pct": float(ic.get("slippage_pct", 0.02)),
        "partial_r": float(ic.get("partial_r", 1.5)),
        "final_r": ic.get("final_r", 3.0),
    }


def backtest(bars_by_symbol: dict[str, pd.DataFrame], cfg: Any,
             overrides: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], list[Event]]:
    """Every session after the first (which only warms the indicators up)."""
    costs = (cfg.get("intraday", {}) or {}).get("costs", {}) or {}
    results, all_events = [], []
    for symbol, bars in bars_by_symbol.items():
        if bars is None or bars.empty:
            continue
        f = with_indicators(bars)
        days = sorted({ts.date() for ts in f.index})
        for day in days[1:]:
            events, trade = session_events(f, day, symbol, {**params_for(symbol, cfg), **(overrides or {})})
            all_events += events
            if trade is not None:
                results.append(result(trade, costs))
    results.sort(key=lambda r: r["entry_at"])
    return results, all_events
