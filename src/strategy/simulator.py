"""A trade simulator for testing entry rules against exit policies.

Built for two questions the older `backtest.run_backtest` cannot answer: it
fills at the signal bar's close, charges nothing, and exits whole positions
only, so it can test neither a swing trade entered at the next morning's
open nor the long-term alerts' staged +25%/+50% trims.

The model, stated because a backtest that hides its assumptions is worse
than none:

* **Signals** are read at a bar's close, from data up to that bar.
* **Fills.** In ``next_open`` mode - the one an 08:00 alert allows - entries
  and rule-based exits fill at the next bar's open. In ``same_close`` mode
  they fill at the signal bar's close, which needs acting minutes before
  15:30.
* **Stops and targets** are resting orders: a stop fills at the stop, or at
  the open if the bar gaps through it; a target fills at the target, or at
  the open if the bar gaps above it. When one bar touches both, the stop is
  assumed first - the pessimistic reading, since daily bars cannot say which
  came first.
* **Costs** on every fill from `charges.estimate` (Groww's schedule), plus
  slippage on market fills. Limit fills at a target pay no slippage.
* **A trade is a win only if it made money after all of that.**

Positions are whole shares. Each stock holds at most one trade at a time.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Protocol

import numpy as np
import pandas as pd

from src import charges as charge_model

NEXT_OPEN = "next_open"
SAME_CLOSE = "same_close"


# --- Data --------------------------------------------------------------------------


@dataclass
class Bars:
    """One stock's history as arrays, for speed; plus any extra columns."""

    symbol: str
    dates: list[date]
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    extra: dict[str, np.ndarray] = field(default_factory=dict)
    position: dict[date, int] = field(default_factory=dict)

    @classmethod
    def from_frame(cls, symbol: str, frame: pd.DataFrame, extra: tuple[str, ...] = ()) -> "Bars":
        dates = [d.date() for d in frame.index]
        return cls(
            symbol=symbol,
            dates=dates,
            open=frame["open"].to_numpy(dtype=float),
            high=frame["high"].to_numpy(dtype=float),
            low=frame["low"].to_numpy(dtype=float),
            close=frame["close"].to_numpy(dtype=float),
            extra={c: frame[c].to_numpy(dtype=float) for c in extra if c in frame.columns},
            position={d: i for i, d in enumerate(dates)},
        )

    def get(self, column: str, i: int) -> float:
        value = self.extra[column][i]
        return float(value)


@dataclass
class Fill:
    when: date
    side: str          # BUY | SELL
    quantity: int
    price: float
    charges: float
    reason: str


@dataclass
class Trade:
    symbol: str
    strategy: str
    signal_date: date
    atr: float = 0.0
    stop: float | None = None
    target: float | None = None
    fills: list[Fill] = field(default_factory=list)
    sessions: int = 0
    tranches_done: int = 0
    peak: float = 0.0            # highest high since entry
    trough: float = 0.0          # lowest low since entry
    peak_close: float = 0.0
    exit_reason: str = ""
    regime: str | None = None
    state: dict[str, Any] = field(default_factory=dict)

    # -- derived
    @property
    def bought(self) -> int:
        return sum(f.quantity for f in self.fills if f.side == "BUY")

    @property
    def sold(self) -> int:
        return sum(f.quantity for f in self.fills if f.side == "SELL")

    @property
    def quantity(self) -> int:
        return self.bought - self.sold

    @property
    def closed(self) -> bool:
        return self.bought > 0 and self.quantity == 0

    @property
    def cost(self) -> float:
        return sum(f.quantity * f.price + f.charges for f in self.fills if f.side == "BUY")

    @property
    def proceeds(self) -> float:
        return sum(f.quantity * f.price - f.charges for f in self.fills if f.side == "SELL")

    @property
    def charges(self) -> float:
        return sum(f.charges for f in self.fills)

    @property
    def pnl(self) -> float:
        return self.proceeds - self.cost

    @property
    def return_pct(self) -> float:
        return self.pnl / self.cost * 100.0 if self.cost > 0 else 0.0

    @property
    def is_win(self) -> bool:
        return self.closed and self.pnl > 0

    @property
    def avg_entry(self) -> float:
        buys = [f for f in self.fills if f.side == "BUY"]
        quantity = sum(f.quantity for f in buys)
        return sum(f.quantity * f.price for f in buys) / quantity if quantity else 0.0

    @property
    def first_price(self) -> float:
        return next((f.price for f in self.fills if f.side == "BUY"), 0.0)

    @property
    def entry_date(self) -> date | None:
        return self.fills[0].when if self.fills else None

    @property
    def exit_date(self) -> date | None:
        return self.fills[-1].when if self.closed else None

    @property
    def mfe_pct(self) -> float:
        entry = self.avg_entry
        return (self.peak - entry) / entry * 100.0 if entry > 0 and self.peak > 0 else 0.0

    @property
    def mae_pct(self) -> float:
        entry = self.avg_entry
        return (self.trough - entry) / entry * 100.0 if entry > 0 and self.trough > 0 else 0.0

    def to_row(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "strategy": self.strategy,
            "signal_date": self.signal_date,
            "entry_date": self.entry_date,
            "exit_date": self.exit_date,
            "avg_entry": round(self.avg_entry, 2),
            "quantity": self.bought,
            "stop": self.stop,
            "target": self.target,
            "exit_reason": self.exit_reason,
            "sessions": self.sessions,
            "cost": round(self.cost, 2),
            "charges": round(self.charges, 2),
            "pnl": round(self.pnl, 2),
            "return_pct": round(self.return_pct, 3),
            "is_win": self.is_win,
            "mfe_pct": round(self.mfe_pct, 2),
            "mae_pct": round(self.mae_pct, 2),
            "regime": self.regime,
        }


# --- Orders ------------------------------------------------------------------------


@dataclass
class Order:
    side: str                     # BUY | SELL
    reason: str
    quantity: int | None = None   # None on a SELL means everything held
    fraction: float | None = None # of the planned position value, for scale-in BUYs
    price: float | None = None    # a resting stop/target level; None = market
    kind: str = "market"          # market | stop | target


class Policy(Protocol):
    """How a position is managed once open. One instance per trade."""

    def on_open(self, trade: Trade, bars: Bars, i: int) -> None: ...

    def intraday(self, trade: Trade, bars: Bars, i: int) -> list[Order]: ...

    def at_close(self, trade: Trade, bars: Bars, i: int) -> list[Order]: ...


# --- The engine --------------------------------------------------------------------


@dataclass
class SimResult:
    trades: list[Trade]
    equity: pd.Series
    exposure: float
    initial_capital: float
    mode: str

    @property
    def closed(self) -> list[Trade]:
        return [t for t in self.trades if t.closed]


def _slip(price: float, side: str, slippage: float) -> float:
    return price * (1 + slippage) if side == "BUY" else price * (1 - slippage)


def simulate(
    bars: dict[str, Bars],
    signals: dict[str, np.ndarray],
    ranks: dict[str, np.ndarray],
    policy: Callable[[str], Policy],
    *,
    strategy: str,
    start: date,
    end: date,
    mode: str = NEXT_OPEN,
    position_value: float = 50_000.0,
    max_positions: int | None = None,
    initial_capital: float | None = None,
    first_tranche: float = 1.0,
    slippage: float = 0.0005,
    cfg: Any = None,
    regime_at: Callable[[date], str | None] | None = None,
    with_costs: bool = True,
) -> SimResult:
    """Walk the calendar, entering on signals and managing trades by policy.

    `max_positions=None` runs every stock independently with unlimited cash:
    the trade statistics then describe the rule itself, not whichever subset
    a portfolio happened to have room for. With a number, it is a portfolio
    with finite cash, taking the lowest-ranked signals first.
    """
    if mode not in (NEXT_OPEN, SAME_CLOSE):
        raise ValueError(f"unknown fill mode {mode!r}")

    unconstrained = max_positions is None
    capital = initial_capital if initial_capital is not None else position_value * (max_positions or 1)
    cash = math.inf if unconstrained else float(capital)

    calendar = sorted({d for b in bars.values() for d in b.dates if start <= d <= end})
    open_trades: dict[str, Trade] = {}
    policies: dict[str, Policy] = {}
    pending_exits: dict[str, list[Order]] = {}
    pending_entries: list[tuple[float, str, int]] = []
    last_close: dict[str, float] = {}
    trades: list[Trade] = []
    equity: list[tuple[date, float]] = []
    days_exposed = 0

    def cost_of(side: str, qty: int, price: float) -> float:
        if not with_costs or qty <= 0:
            return 0.0
        return charge_model.estimate(side, qty, price, cfg=cfg).total

    def fill(trade: Trade, side: str, qty: int, price: float, when: date, reason: str) -> None:
        nonlocal cash
        if qty <= 0:
            return
        charges = cost_of(side, qty, price)
        trade.fills.append(Fill(when, side, qty, round(price, 4), charges, reason))
        if not unconstrained:
            cash += (qty * price - charges) if side == "SELL" else -(qty * price + charges)

    def execute(symbol: str, order: Order, b: Bars, i: int, price: float) -> None:
        trade = open_trades[symbol]
        when = b.dates[i]
        if order.side == "SELL":
            qty = trade.quantity if order.quantity is None else min(order.quantity, trade.quantity)
            slip = 0.0 if order.kind == "target" or not with_costs else slippage
            fill(trade, "SELL", qty, _slip(price, "SELL", slip), when, order.reason)
            if trade.quantity == 0:
                trade.exit_reason = order.reason
                trades.append(trade)
                del open_trades[symbol]
                policies.pop(symbol, None)
                pending_exits.pop(symbol, None)
        else:  # scale-in BUY
            value = position_value * (order.fraction or 0.0)
            buy_price = _slip(price, "BUY", slippage if with_costs else 0.0)
            qty = int(value // buy_price) if buy_price > 0 else 0
            if not unconstrained and qty * buy_price + cost_of("BUY", qty, buy_price) > cash:
                return
            fill(trade, "BUY", qty, buy_price, when, order.reason)
            trade.tranches_done += 1

    def open_trade(symbol: str, b: Bars, signal_i: int, fill_i: int, price: float) -> None:
        buy_price = _slip(price, "BUY", slippage if with_costs else 0.0)
        value = position_value * first_tranche
        qty = int(value // buy_price) if buy_price > 0 else 0
        if qty < 1:
            return
        if not unconstrained and qty * buy_price + cost_of("BUY", qty, buy_price) > cash:
            return
        atr = float(b.extra["atr"][signal_i]) if "atr" in b.extra else 0.0
        trade = Trade(
            symbol=symbol, strategy=strategy, signal_date=b.dates[signal_i],
            atr=0.0 if math.isnan(atr) else atr,
            peak=buy_price, trough=buy_price, peak_close=buy_price,
            regime=regime_at(b.dates[signal_i]) if regime_at else None,
        )
        open_trades[symbol] = trade
        fill(trade, "BUY", qty, buy_price, b.dates[fill_i], "entry")
        trade.tranches_done = 1
        policies[symbol] = policy(symbol)
        policies[symbol].on_open(trade, b, fill_i)

    def run_orders(symbol: str, orders: list[Order], b: Bars, i: int, at: str) -> None:
        for order in orders:
            if symbol not in open_trades:
                return
            if at == "open":
                price = b.open[i]
            elif at == "close":
                price = b.close[i]
            else:   # intraday resting order
                if order.kind == "stop":
                    price = min(b.open[i], order.price)
                else:
                    price = max(b.open[i], order.price)
            execute(symbol, order, b, i, price)

    for day in calendar:
        exited_today: set[str] = set()

        # 1. Yesterday's close decisions, filled at today's open.
        if mode == NEXT_OPEN:
            for symbol in list(pending_exits):
                b = bars[symbol]
                i = b.position.get(day)
                if i is None or symbol not in open_trades:
                    continue
                orders = pending_exits.pop(symbol)
                run_orders(symbol, orders, b, i, "open")
                if symbol not in open_trades:
                    exited_today.add(symbol)

            for _, symbol, signal_i in sorted(pending_entries):
                if symbol in open_trades:
                    continue
                if not unconstrained and len(open_trades) >= max_positions:
                    break
                b = bars[symbol]
                i = b.position.get(day)
                if i is None:
                    continue
                open_trade(symbol, b, signal_i, i, b.open[i])
            pending_entries = []

        # 2. Resting stops and targets during the session.
        for symbol in list(open_trades):
            b = bars[symbol]
            i = b.position.get(day)
            if i is None:
                continue
            trade = open_trades[symbol]
            # A same-close entry has not been through a session yet.
            if mode == SAME_CLOSE and trade.fills[0].when == day:
                continue
            orders = policies[symbol].intraday(trade, b, i)
            if orders:
                run_orders(symbol, orders, b, i, "intraday")
                if symbol not in open_trades:
                    exited_today.add(symbol)

        # 3. The close: excursions, rule exits, time stops, scale-ins.
        for symbol in list(open_trades):
            b = bars[symbol]
            i = b.position.get(day)
            if i is None:
                continue
            trade = open_trades[symbol]
            same_bar_entry = mode == SAME_CLOSE and trade.fills[0].when == day
            if not same_bar_entry:
                trade.sessions += 1
                trade.peak = max(trade.peak, b.high[i])
                trade.trough = min(trade.trough, b.low[i])
                trade.peak_close = max(trade.peak_close, b.close[i])
            orders = [] if same_bar_entry else policies[symbol].at_close(trade, b, i)
            if not orders:
                continue
            if mode == SAME_CLOSE:
                run_orders(symbol, orders, b, i, "close")
                if symbol not in open_trades:
                    exited_today.add(symbol)
            else:
                pending_exits[symbol] = orders

        # 4. New signals at the close.
        candidates: list[tuple[float, str, int]] = []
        for symbol, b in bars.items():
            if symbol in open_trades or symbol in exited_today or symbol in pending_exits:
                continue
            i = b.position.get(day)
            if i is None or not signals[symbol][i]:
                continue
            if mode == NEXT_OPEN and i + 1 >= len(b.dates):
                continue
            rank = ranks[symbol][i]
            candidates.append((0.0 if np.isnan(rank) else float(rank), symbol, i))

        if mode == SAME_CLOSE:
            for _, symbol, i in sorted(candidates):
                if not unconstrained and len(open_trades) >= max_positions:
                    break
                open_trade(symbol, bars[symbol], i, i, bars[symbol].close[i])
        else:
            pending_entries = candidates

        # 5. Mark to market.
        for symbol, b in bars.items():
            i = b.position.get(day)
            if i is not None:
                last_close[symbol] = float(b.close[i])
        if open_trades:
            days_exposed += 1
        if not unconstrained:
            held = sum(t.quantity * last_close.get(s, t.avg_entry) for s, t in open_trades.items())
            equity.append((day, cash + held))

    # Anything still open is closed at the last price seen, and flagged.
    for symbol, trade in list(open_trades.items()):
        b = bars[symbol]
        i = max(k for k, d in enumerate(b.dates) if d <= end)
        fill(trade, "SELL", trade.quantity, float(b.close[i]), b.dates[i], "end_of_test")
        trade.exit_reason = "end_of_test"
        trades.append(trade)

    curve = pd.Series(dict(equity), dtype=float) if equity else pd.Series(dtype=float)
    return SimResult(
        trades=trades, equity=curve,
        exposure=days_exposed / len(calendar) if calendar else 0.0,
        initial_capital=float(capital), mode=mode,
    )


# --- Statistics ----------------------------------------------------------------------


def trade_stats(trades: list[Trade]) -> dict[str, Any]:
    """What a set of closed trades says about a rule, after costs."""
    closed = [t for t in trades if t.closed and t.exit_reason != "end_of_test"]
    if not closed:
        return {"trades": 0}

    returns = np.array([t.return_pct for t in closed])
    pnls = np.array([t.pnl for t in closed])
    wins = pnls > 0
    gross_win = pnls[wins].sum()
    gross_loss = -pnls[~wins].sum()

    by_year: dict[int, dict[str, float]] = {}
    for t in closed:
        year = t.entry_date.year
        entry = by_year.setdefault(year, {"trades": 0, "wins": 0, "pnl": 0.0})
        entry["trades"] += 1
        entry["wins"] += int(t.is_win)
        entry["pnl"] += t.pnl
    for entry in by_year.values():
        entry["win_rate"] = entry["wins"] / entry["trades"]

    by_exit: dict[str, dict[str, float]] = {}
    for t in closed:
        entry = by_exit.setdefault(t.exit_reason, {"trades": 0, "sum": 0.0})
        entry["trades"] += 1
        entry["sum"] += t.return_pct
    for entry in by_exit.values():
        entry["avg_return_pct"] = entry.pop("sum") / entry["trades"]

    by_regime: dict[str, dict[str, float]] = {}
    for t in closed:
        entry = by_regime.setdefault(t.regime or "UNKNOWN", {"trades": 0, "wins": 0, "sum": 0.0})
        entry["trades"] += 1
        entry["wins"] += int(t.is_win)
        entry["sum"] += t.return_pct
    for entry in by_regime.values():
        entry["win_rate"] = entry["wins"] / entry["trades"]
        entry["avg_return_pct"] = entry.pop("sum") / entry["trades"]

    return {
        "trades": len(closed),
        "win_rate": float(wins.mean()),
        "avg_win_pct": float(returns[wins].mean()) if wins.any() else 0.0,
        "avg_loss_pct": float(returns[~wins].mean()) if (~wins).any() else 0.0,
        "profit_factor": float(gross_win / gross_loss) if gross_loss > 0 else math.inf,
        "expectancy_pct": float(returns.mean()),
        "expectancy_rs": float(pnls.mean()),
        "total_pnl": float(pnls.sum()),
        "worst_pct": float(returns.min()),
        "best_pct": float(returns.max()),
        "avg_sessions": float(np.mean([t.sessions for t in closed])),
        "avg_charges": float(np.mean([t.charges for t in closed])),
        "by_year": by_year,
        "by_exit": by_exit,
        "by_regime": by_regime,
    }


def curve_stats(curve: pd.Series, initial: float) -> dict[str, float]:
    """Growth and worst fall of an equity curve."""
    if curve is None or len(curve) < 2 or initial <= 0:
        return {"cagr_pct": 0.0, "max_drawdown_pct": 0.0, "final": initial}
    first, last = curve.index[0], curve.index[-1]
    years = max((last - first).days / 365.25, 1e-9)
    final = float(curve.iloc[-1])
    cagr = ((final / initial) ** (1 / years) - 1) * 100.0 if final > 0 else -100.0
    running_peak = curve.cummax()
    drawdown = ((curve - running_peak) / running_peak).min() * 100.0
    return {"cagr_pct": float(cagr), "max_drawdown_pct": float(drawdown), "final": final}
