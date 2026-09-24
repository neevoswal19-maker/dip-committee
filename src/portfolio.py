"""The portfolio ledger: lots, FIFO matching, and derived position state.

Transactions are the truth. Quantity, average cost and open/closed status are
computed from them, never stored as independent facts that can drift.

**Why FIFO.** Indian income-tax rules match sales of listed equity against the
earliest purchases first. It is not a modelling preference you can swap for
average cost - it determines which shares you actually sold, and therefore
whether a given sale is short-term or long-term. Since this system already
shows you the rupee difference between the two, getting the matching wrong
would make that number confidently false.

**Why charges are in the cost basis.** A purchase costs more than the traded
price and a sale returns less. Measuring returns on the traded price quietly
flatters every result, and those results feed the sizer's win rate and payoff
estimates. Both a gross and a net figure are kept so the difference stays
visible rather than assumed.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Literal

from sqlalchemy import and_, delete, or_, select

from src import charges as charge_model
from src import db
from src.config import load_config

log = logging.getLogger(__name__)

Side = Literal["BUY", "SELL"]


#: The two strategies a position can belong to. Kept apart so each is judged,
#: sized and exited by its own rules, and labelled everywhere it appears.
LONG_TERM = "long_term"
SWING = "swing"
STRATEGIES = (LONG_TERM, SWING)
LABELS = {LONG_TERM: "LONG-TERM", SWING: "SWING"}


class LedgerError(ValueError):
    """Raised when a transaction would make the ledger incoherent."""


# --- Lots -------------------------------------------------------------------


@dataclass
class Lot:
    """A parcel of shares from one purchase, with what remains of it.

    Each lot carries its own acquisition date, which is what makes the tax
    clock correct: three tranches bought months apart reach long-term
    treatment on three different days.
    """

    transaction_id: int
    trade_date: date
    quantity: float          # original size
    remaining: float         # after FIFO consumption by later sells
    price: float             # traded price
    cost_per_share: float    # traded price plus that lot's share of charges
    tranche_label: str | None = None

    def ltcg_date(self, ltcg_days: int = 366) -> date:
        return self.trade_date + timedelta(days=ltcg_days)

    def days_held(self, as_of: date | None = None) -> int:
        return ((as_of or date.today()) - self.trade_date).days

    def days_to_ltcg(self, ltcg_days: int = 366, as_of: date | None = None) -> int:
        return max(0, ltcg_days - self.days_held(as_of))

    def is_long_term(self, ltcg_days: int = 366, as_of: date | None = None) -> bool:
        return self.days_held(as_of) >= ltcg_days

    @property
    def cost(self) -> float:
        return round(self.remaining * self.cost_per_share, 2)


@dataclass
class Disposal:
    """One FIFO match: a slice of a sell against a slice of a buy lot."""

    quantity: float
    buy_date: date
    buy_cost_per_share: float
    sell_date: date
    sell_price_per_share: float   # net of that sale's charges
    holding_days: int
    is_long_term: bool

    @property
    def proceeds(self) -> float:
        return round(self.quantity * self.sell_price_per_share, 2)

    @property
    def cost(self) -> float:
        return round(self.quantity * self.buy_cost_per_share, 2)

    @property
    def gain(self) -> float:
        return round(self.proceeds - self.cost, 2)


@dataclass
class PositionState:
    """Everything derived from a position's transactions."""

    position_id: int
    symbol: str
    quantity: float = 0.0
    avg_cost: float = 0.0            # net of charges, FIFO-consistent
    avg_price: float = 0.0           # traded price only, for comparison
    invested: float = 0.0
    status: str = "closed"
    first_buy_date: date | None = None
    last_trade_date: date | None = None
    lots: list[Lot] = field(default_factory=list)
    disposals: list[Disposal] = field(default_factory=list)
    realised_gain: float = 0.0
    realised_short_term: float = 0.0
    realised_long_term: float = 0.0
    total_charges: float = 0.0
    trims_taken: list[float] = field(default_factory=list)
    strategy: str = "long_term"
    stop_price: float | None = None

    @property
    def is_open(self) -> bool:
        return self.quantity > 1e-9

    def market_value(self, price: float) -> float:
        return round(self.quantity * price, 2)

    def unrealised(self, price: float) -> float:
        return round(self.quantity * (price - self.avg_cost), 2)

    def gain_pct(self, price: float) -> float:
        if self.avg_cost <= 0:
            return 0.0
        return (price - self.avg_cost) / self.avg_cost * 100.0


# --- The matcher ------------------------------------------------------------


def match_fifo(
    buys: list[dict[str, Any]],
    sells: list[dict[str, Any]],
    *,
    ltcg_days: int = 366,
    strict: bool = True,
) -> tuple[list[Lot], list[Disposal]]:
    """Consume buy lots with sells, oldest first.

    Returns the remaining lots and the realised disposals. `strict` rejects a
    sale larger than the shares on hand; the importer relaxes it, because a
    broker file may legitimately start mid-history with sales of shares
    bought before the export window.
    """
    lots: list[Lot] = []
    for row in sorted(buys, key=lambda r: (r["trade_date"], r["id"])):
        quantity = float(row["quantity"])
        lots.append(
            Lot(
                transaction_id=int(row["id"]),
                trade_date=row["trade_date"],
                quantity=quantity,
                remaining=quantity,
                price=float(row["price"]),
                cost_per_share=_cost_per_share(row),
                tranche_label=row.get("tranche_label"),
            )
        )

    disposals: list[Disposal] = []

    for sale in sorted(sells, key=lambda r: (r["trade_date"], r["id"])):
        outstanding = float(sale["quantity"])
        net_per_share = _proceeds_per_share(sale)

        for lot in lots:
            if outstanding <= 1e-9:
                break
            if lot.remaining <= 1e-9:
                continue

            taken = min(lot.remaining, outstanding)
            lot.remaining = round(lot.remaining - taken, 6)
            outstanding = round(outstanding - taken, 6)

            held = (sale["trade_date"] - lot.trade_date).days
            disposals.append(
                Disposal(
                    quantity=taken,
                    buy_date=lot.trade_date,
                    buy_cost_per_share=lot.cost_per_share,
                    sell_date=sale["trade_date"],
                    sell_price_per_share=net_per_share,
                    holding_days=held,
                    is_long_term=held >= ltcg_days,
                )
            )

        if outstanding > 1e-6:
            message = (
                f"sale of {sale['quantity']:g} {sale.get('symbol', '')} on "
                f"{sale['trade_date']} exceeds shares held by {outstanding:g}"
            )
            if strict:
                raise LedgerError(message)
            log.warning("%s - ignoring the excess", message)

    return [lot for lot in lots if lot.remaining > 1e-9], disposals


def _cost_per_share(row: dict[str, Any]) -> float:
    """Traded price plus this purchase's share of its charges."""
    quantity = float(row["quantity"])
    if quantity <= 0:
        return 0.0
    net = row.get("net_amount")
    if net is None:
        net = quantity * float(row["price"]) + float(row.get("total_charges") or 0.0)
    return round(float(net) / quantity, 6)


def _proceeds_per_share(row: dict[str, Any]) -> float:
    """Traded price less this sale's share of its charges."""
    quantity = float(row["quantity"])
    if quantity <= 0:
        return 0.0
    net = row.get("net_amount")
    if net is None:
        net = quantity * float(row["price"]) - float(row.get("total_charges") or 0.0)
    return round(float(net) / quantity, 6)


# --- Reading ----------------------------------------------------------------


def transactions_for(position_id: int) -> list[dict[str, Any]]:
    with db.connection() as conn:
        rows = conn.execute(
            select(db.transactions)
            .where(db.transactions.c.position_id == position_id)
            .order_by(db.transactions.c.trade_date, db.transactions.c.id)
        ).fetchall()
    return [dict(r._mapping) for r in rows]


def all_transactions(symbol: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
    query = select(db.transactions).order_by(
        db.transactions.c.trade_date.desc(), db.transactions.c.id.desc()
    ).limit(limit)
    if symbol:
        query = query.where(db.transactions.c.symbol == symbol.upper())
    with db.connection() as conn:
        return [dict(r._mapping) for r in conn.execute(query).fetchall()]


def state_for(position_id: int, *, cfg: Any = None) -> PositionState:
    """Derive everything about a position from its transactions."""
    cfg = cfg or load_config()
    ltcg_days = int(cfg.get("exit.tax.ltcg_days", 366))

    with db.connection() as conn:
        row = conn.execute(
            select(db.positions).where(db.positions.c.id == position_id)
        ).first()
    if row is None:
        raise LedgerError(f"no position {position_id}")

    record = dict(row._mapping)
    rows = transactions_for(position_id)

    buys = [r for r in rows if str(r["side"]).upper() == "BUY"]
    sells = [r for r in rows if str(r["side"]).upper() == "SELL"]

    lots, disposals = match_fifo(buys, sells, ltcg_days=ltcg_days, strict=False)

    quantity = round(sum(lot.remaining for lot in lots), 6)
    cost = sum(lot.remaining * lot.cost_per_share for lot in lots)
    gross = sum(lot.remaining * lot.price for lot in lots)

    state = PositionState(
        position_id=position_id,
        symbol=record["symbol"],
        quantity=quantity,
        avg_cost=round(cost / quantity, 4) if quantity > 0 else 0.0,
        avg_price=round(gross / quantity, 4) if quantity > 0 else 0.0,
        invested=round(cost, 2),
        status="open" if quantity > 1e-9 else "closed",
        first_buy_date=min((r["trade_date"] for r in buys), default=None),
        last_trade_date=max((r["trade_date"] for r in rows), default=None),
        lots=lots,
        disposals=disposals,
        realised_gain=round(sum(d.gain for d in disposals), 2),
        realised_short_term=round(sum(d.gain for d in disposals if not d.is_long_term), 2),
        realised_long_term=round(sum(d.gain for d in disposals if d.is_long_term), 2),
        total_charges=round(sum(float(r.get("total_charges") or 0.0) for r in rows), 2),
        trims_taken=_derive_trims(record, sells),
        strategy=record.get("strategy") or LONG_TERM,
        stop_price=record.get("stop_price"),
    )
    return state


def _derive_trims(position: dict[str, Any], sells: list[dict[str, Any]]) -> list[float]:
    """Which profit targets have already been acted on.

    Derived from the sells themselves rather than stored, so a target cannot
    fire a second time after a restart, and editing a trade corrects the
    history rather than leaving a stale flag behind.
    """
    doctrine = db.from_json(position.get("exit_doctrine_json"), {}) or {}
    targets = doctrine.get("targets") or []
    if not targets or not sells:
        return []

    entry = float(doctrine.get("entry_price") or 0.0)
    if entry <= 0:
        return []

    taken: list[float] = []
    for target in targets:
        level = float(target.get("gain_pct", 0))
        trigger_price = entry * (1 + level / 100.0)
        if any(float(s["price"]) >= trigger_price * 0.98 for s in sells):
            taken.append(level)
    return taken


# --- Writing ----------------------------------------------------------------


def _sync_position(position_id: int, *, cfg: Any = None) -> PositionState:
    """Recompute the positions row from its transactions."""
    state = state_for(position_id, cfg=cfg)

    values: dict[str, Any] = {
        "quantity": state.quantity,
        "avg_entry_price": state.avg_cost,
        "invested_value": state.invested,
        "status": state.status,
        "entry_date": state.first_buy_date,
        "realised_pnl": state.realised_gain,
    }

    if state.status == "closed" and state.disposals:
        last = max(state.disposals, key=lambda d: d.sell_date)
        values["exit_date"] = last.sell_date
        values["exit_price"] = round(last.sell_price_per_share, 2)
    else:
        values["exit_date"] = None
        values["exit_price"] = None

    with db.connection() as conn:
        conn.execute(
            db.positions.update().where(db.positions.c.id == position_id).values(**values)
        )
    return state


def _strategy_clause(strategy: str):
    """Match a strategy, treating NULL (positions from before swing) as long-term."""
    column = db.positions.c.strategy
    if strategy == LONG_TERM:
        return or_(column == LONG_TERM, column.is_(None))
    return column == strategy


def find_open_position(symbol: str, strategy: str | None = None) -> int | None:
    """The open position in `symbol`; in one strategy, if one is named."""
    conditions = [db.positions.c.symbol == symbol.upper(), db.positions.c.status == "open"]
    if strategy is not None:
        conditions.append(_strategy_clause(strategy))
    with db.connection() as conn:
        row = conn.execute(
            select(db.positions.c.id).where(and_(*conditions))
            .order_by(db.positions.c.id.desc()).limit(1)
        ).first()
    return int(row.id) if row else None


def open_strategy_for(symbol: str) -> str | None:
    """Which strategy holds `symbol` right now, if either does."""
    with db.connection() as conn:
        row = conn.execute(
            select(db.positions.c.strategy).where(
                and_(db.positions.c.symbol == symbol.upper(), db.positions.c.status == "open")
            ).order_by(db.positions.c.id.desc()).limit(1)
        ).first()
    if row is None:
        return None
    return row.strategy or LONG_TERM


def create_position(
    symbol: str,
    *,
    conviction: float | None = None,
    recommendation: str | None = None,
    stop_price: float | None = None,
    committee_run_id: int | None = None,
    exit_doctrine: dict[str, Any] | None = None,
    notes: str | None = None,
    strategy: str = "long_term",
) -> int:
    if strategy not in STRATEGIES:
        raise LedgerError(f"unknown strategy {strategy!r}")
    held_in = open_strategy_for(symbol)
    if held_in is not None and held_in != strategy:
        raise LedgerError(
            f"{symbol.upper()} is already held as a {LABELS[held_in]} position. Tax rules sell the "
            f"oldest shares first (FIFO), so selling the {LABELS[strategy]} shares would really "
            f"sell the {LABELS[held_in]} ones - one stock can only be in one strategy at a time."
        )
    with db.connection() as conn:
        return conn.execute(
            db.positions.insert().values(
                symbol=symbol.upper(),
                status="open",
                strategy=strategy,
                conviction=conviction,
                recommendation=recommendation,
                stop_price=stop_price,
                committee_run_id=committee_run_id,
                exit_doctrine_json=db.to_json(exit_doctrine) if exit_doctrine else None,
                notes=notes,
                quantity=0.0,
            )
        ).inserted_primary_key[0]


def synthetic_order_id(
    symbol: str, side: str, trade_date: date, quantity: float, price: float
) -> str:
    """A stable id for a trade whose source gave none.

    Deterministic, so re-importing the same file produces the same id and the
    unique constraint rejects the duplicate.
    """
    raw = f"{symbol.upper()}|{side.upper()}|{trade_date}|{quantity:.4f}|{price:.4f}"
    return "syn_" + hashlib.sha256(raw.encode()).hexdigest()[:20]


def _charge_breakdown(
    side: str,
    quantity: float,
    price: float,
    charges: dict[str, float] | charge_model.ChargeBreakdown | None,
    *,
    cfg: Any,
    broker: str | None,
) -> dict[str, float]:
    """Charges as a dict with a `total`, estimating them when none are given."""
    if isinstance(charges, charge_model.ChargeBreakdown):
        return charges.to_dict()
    if charges is None:
        return charge_model.estimate(side, quantity, price, cfg=cfg, broker=broker).to_dict()
    breakdown = dict(charges)
    breakdown.setdefault(
        "total",
        round(sum(v for k, v in breakdown.items() if k != "total" and isinstance(v, (int, float))), 2),
    )
    return breakdown


def _charge_columns(breakdown: dict[str, float]) -> dict[str, float]:
    return {
        "brokerage": breakdown.get("brokerage", 0.0),
        "stt": breakdown.get("stt", 0.0),
        "exchange_fee": breakdown.get("exchange_fee", 0.0),
        "sebi_fee": breakdown.get("sebi_fee", 0.0),
        "stamp_duty": breakdown.get("stamp_duty", 0.0),
        "gst": breakdown.get("gst", 0.0),
        "dp_charge": breakdown.get("dp_charge", 0.0),
        "other_charges": breakdown.get("other", 0.0),
        "total_charges": float(breakdown.get("total", 0.0)),
    }


def record_trade(
    symbol: str,
    side: Side,
    trade_date: date,
    quantity: float,
    price: float,
    *,
    position_id: int | None = None,
    charges: dict[str, float] | charge_model.ChargeBreakdown | None = None,
    broker: str | None = None,
    order_id: str | None = None,
    exchange: str = "NSE",
    tranche_label: str | None = None,
    notes: str | None = None,
    source: str = "manual",
    cfg: Any = None,
    create_if_missing: bool = True,
    strategy: str = "long_term",
) -> tuple[int, PositionState]:
    """Record one buy or sell and resync the position it belongs to.

    Charges default to the estimator when not supplied, so a quick entry is
    still costed rather than silently free.
    """
    cfg = cfg or load_config()
    symbol = symbol.upper()
    side = str(side).upper()  # type: ignore[assignment]

    if quantity <= 0:
        raise LedgerError("quantity must be positive")
    if price <= 0:
        raise LedgerError("price must be positive")

    breakdown = _charge_breakdown(side, quantity, price, charges, cfg=cfg, broker=broker)
    total_charges = float(breakdown.get("total", 0.0))

    if position_id is None:
        position_id = find_open_position(symbol, strategy)
        if position_id is None:
            if side == "SELL":
                raise LedgerError(f"cannot sell {symbol}: no open position ({LABELS.get(strategy, strategy)})")
            if not create_if_missing:
                raise LedgerError(f"no open position for {symbol}")
            position_id = create_position(symbol, strategy=strategy)

    if side == "SELL":
        held = state_for(position_id, cfg=cfg).quantity
        if quantity > held + 1e-6:
            raise LedgerError(
                f"cannot sell {quantity:g} {symbol}: only {held:g} held"
            )

    gross = round(quantity * price, 2)
    net = charge_model.net_amount(side, quantity, price, total_charges)

    with db.connection() as conn:
        conn.execute(
            db.transactions.insert().values(
                position_id=position_id,
                symbol=symbol,
                side=side,
                trade_date=trade_date,
                quantity=float(quantity),
                price=float(price),
                **_charge_columns(breakdown),
                gross_amount=gross,
                net_amount=net,
                broker=(broker or cfg.get("charges.default_broker", "manual")),
                order_id=order_id or synthetic_order_id(symbol, side, trade_date, quantity, price),
                exchange=exchange,
                tranche_label=tranche_label,
                notes=notes,
                source=source,
            )
        )

    state = _sync_position(position_id, cfg=cfg)

    if state.status == "closed":
        _write_trade_record(position_id, state, cfg)

    return position_id, state


def delete_transaction(transaction_id: int, *, cfg: Any = None) -> None:
    """Remove a transaction and resync. Used to correct a mistyped entry."""
    with db.connection() as conn:
        row = conn.execute(
            select(db.transactions.c.position_id).where(db.transactions.c.id == transaction_id)
        ).first()
        if row is None:
            raise LedgerError(f"no transaction {transaction_id}")
        position_id = row.position_id
        conn.execute(delete(db.transactions).where(db.transactions.c.id == transaction_id))

    if position_id is None:
        return

    if not transactions_for(position_id) and _remove_position_if_unreferenced(position_id):
        return

    state = _sync_position(position_id, cfg=cfg)
    # Removing a sale can reopen a position whose closed-trade record was
    # already written. Left in place, that record is a win or loss that never
    # happened, and the learning loop would be trained on it.
    _refresh_trade_record(position_id, state, cfg)


def amend_transaction(
    transaction_id: int,
    *,
    price: float,
    trade_date: date,
    charges: dict[str, float] | charge_model.ChargeBreakdown | None = None,
    broker: str | None = None,
    order_id: str | None = None,
    source: str = "import",
    notes: str | None = None,
    cfg: Any = None,
) -> PositionState:
    """Correct a recorded trade in place and resync its position.

    Used when a broker import supersedes an estimate - a Telegram entry
    priced from the market at the minute it was sent. Amending rather than
    deleting and re-inserting keeps the position, its conviction and exit
    doctrine, and the trade's place in FIFO order.
    """
    cfg = cfg or load_config()
    with db.connection() as conn:
        row = conn.execute(
            select(db.transactions).where(db.transactions.c.id == transaction_id)
        ).first()
    if row is None:
        raise LedgerError(f"no transaction {transaction_id}")
    if price <= 0:
        raise LedgerError("price must be positive")

    record = dict(row._mapping)
    side, quantity = str(record["side"]).upper(), float(record["quantity"])
    breakdown = _charge_breakdown(side, quantity, price, charges, cfg=cfg, broker=broker)

    values: dict[str, Any] = {
        "price": float(price),
        "trade_date": trade_date,
        **_charge_columns(breakdown),
        "gross_amount": round(quantity * price, 2),
        "net_amount": charge_model.net_amount(side, quantity, price, float(breakdown.get("total", 0.0))),
        "source": source,
        "notes": notes,
    }
    if broker:
        values["broker"] = broker
    if order_id:
        values["order_id"] = order_id

    with db.connection() as conn:
        conn.execute(
            db.transactions.update().where(db.transactions.c.id == transaction_id).values(**values)
        )

    state = _sync_position(record["position_id"], cfg=cfg)
    _refresh_trade_record(record["position_id"], state, cfg)
    return state


def _refresh_trade_record(position_id: int, state: PositionState, cfg: Any) -> None:
    """Make the closed-trade record match the position as it now stands."""
    with db.connection() as conn:
        existing = conn.execute(
            select(db.trades.c.id).where(db.trades.c.position_id == position_id)
        ).fetchall()
        for trade in existing:
            referenced = conn.execute(
                select(db.lessons.c.id).where(db.lessons.c.trade_id == trade.id).limit(1)
            ).first()
            if referenced:
                # A lesson was drawn from this trade; keep both rather than
                # orphan the lesson. Rare, and visible in the log.
                log.warning("Trade %s has lessons attached; left in place", trade.id)
                return
            conn.execute(delete(db.trades).where(db.trades.c.id == trade.id))

    if state.status == "closed":
        _write_trade_record(position_id, state, cfg)


def _remove_position_if_unreferenced(position_id: int) -> bool:
    """Delete a position left with no transactions, if nothing else points at it.

    Undoing the only purchase of a stock should leave no trace, not an empty
    closed position in the history.
    """
    with db.connection() as conn:
        for table in (db.transactions, db.trades, db.exit_signals, db.position_events):
            if conn.execute(
                select(table.c.id).where(table.c.position_id == position_id).limit(1)
            ).first():
                return False
        conn.execute(delete(db.positions).where(db.positions.c.id == position_id))
    return True


def _write_trade_record(position_id: int, state: PositionState, cfg: Any) -> None:
    """Add the closed position to the learning ledger.

    Written on full close only. Returns are net of charges, because that is
    the number the sizer's win rate and payoff estimates should be built on.
    """
    if not state.disposals:
        return

    with db.connection() as conn:
        existing = conn.execute(
            select(db.trades.c.id).where(db.trades.c.position_id == position_id)
        ).first()
        if existing:
            return

        row = conn.execute(
            select(db.positions).where(db.positions.c.id == position_id)
        ).first()

    position = dict(row._mapping) if row else {}

    total_qty = sum(d.quantity for d in state.disposals)
    if total_qty <= 0:
        return

    entry = sum(d.quantity * d.buy_cost_per_share for d in state.disposals) / total_qty
    exit_price = sum(d.quantity * d.sell_price_per_share for d in state.disposals) / total_qty
    entry_date = min(d.buy_date for d in state.disposals)
    exit_date = max(d.sell_date for d in state.disposals)

    return_pct = (exit_price - entry) / entry * 100.0 if entry > 0 else 0.0
    holding_days = (exit_date - entry_date).days

    peak = position.get("peak_price")
    mfe_pct = ((float(peak) - entry) / entry * 100.0) if peak and entry > 0 else 0.0

    with db.connection() as conn:
        conn.execute(
            db.trades.insert().values(
                position_id=position_id,
                committee_run_id=position.get("committee_run_id"),
                symbol=state.symbol,
                entry_date=entry_date,
                exit_date=exit_date,
                entry_price=round(entry, 4),
                exit_price=round(exit_price, 4),
                quantity=total_qty,
                return_pct=round(return_pct, 4),
                holding_days=holding_days,
                is_win=return_pct > 0,
                conviction=position.get("conviction"),
                conviction_band=(position.get("recommendation") or "").lower() or None,
                recommendation=position.get("recommendation"),
                mfe_pct=round(mfe_pct, 4),
                captured_fraction=round(return_pct / mfe_pct, 4) if mfe_pct > 0 else 0.0,
                exit_reason=position.get("exit_reason") or "closed",
                was_ltcg=all(d.is_long_term for d in state.disposals),
            )
        )


# --- Portfolio-wide ---------------------------------------------------------


def open_positions(*, cfg: Any = None, strategy: str | None = None) -> list[PositionState]:
    query = select(db.positions.c.id).where(db.positions.c.status == "open")
    if strategy is not None:
        query = query.where(_strategy_clause(strategy))
    with db.connection() as conn:
        rows = conn.execute(query.order_by(db.positions.c.entry_date.desc())).fetchall()

    states = []
    for row in rows:
        try:
            state = state_for(int(row.id), cfg=cfg)
            if state.is_open:
                states.append(state)
        except LedgerError as exc:
            log.warning("Skipping position %s: %s", row.id, exc)
    return states


def realised_summary(*, financial_year_start: date | None = None) -> dict[str, Any]:
    """Realised gains across every position, split by tax treatment.

    The split matters because the long-term exemption is an annual allowance -
    knowing how much of it is already used changes whether selling now is
    expensive.
    """
    with db.connection() as conn:
        rows = conn.execute(select(db.positions.c.id)).fetchall()

    short_term = long_term = 0.0
    disposals: list[Disposal] = []

    for row in rows:
        try:
            state = state_for(int(row.id))
        except LedgerError:
            continue
        for disposal in state.disposals:
            if financial_year_start and disposal.sell_date < financial_year_start:
                continue
            disposals.append(disposal)
            if disposal.is_long_term:
                long_term += disposal.gain
            else:
                short_term += disposal.gain

    return {
        "short_term_gain": round(short_term, 2),
        "long_term_gain": round(long_term, 2),
        "total_gain": round(short_term + long_term, 2),
        "disposal_count": len(disposals),
    }


def backfill_transactions(*, cfg: Any = None) -> int:
    """Give legacy positions a synthetic opening purchase.

    Positions created before the ledger existed carry a quantity and average
    price but no transactions, so they would derive to zero and vanish. One
    synthetic BUY reproduces the state they were saved with.
    """
    cfg = cfg or load_config()
    converted = 0

    with db.connection() as conn:
        rows = conn.execute(
            select(db.positions).where(db.positions.c.quantity > 0)
        ).fetchall()

    for row in rows:
        position = dict(row._mapping)
        if transactions_for(position["id"]):
            continue

        quantity = float(position.get("quantity") or 0)
        price = float(position.get("avg_entry_price") or 0)
        trade_date = position.get("entry_date") or date.today()
        if quantity <= 0 or price <= 0:
            continue

        with db.connection() as conn:
            conn.execute(
                db.transactions.insert().values(
                    position_id=position["id"],
                    symbol=position["symbol"],
                    side="BUY",
                    trade_date=trade_date,
                    quantity=quantity,
                    price=price,
                    total_charges=0.0,
                    gross_amount=round(quantity * price, 2),
                    net_amount=round(quantity * price, 2),
                    broker="legacy",
                    order_id=synthetic_order_id(
                        position["symbol"], "BUY", trade_date, quantity, price
                    ),
                    tranche_label="backfill",
                    notes="synthesised from a position recorded before the ledger existed",
                    source="backfill",
                )
            )
        _sync_position(position["id"], cfg=cfg)
        converted += 1

    if converted:
        log.info("Backfilled %d legacy positions into the transaction ledger", converted)
    return converted
