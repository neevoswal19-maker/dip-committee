"""Database schema and access.

SQLite locally, Postgres in deployment, one schema for both. Streamlit
Community Cloud has no persistent disk, so a SQLite file there would be
wiped on every restart - DATABASE_URL is what makes the ledger survive.

The centre of gravity here is the trade ledger. Every committee verdict is
stored with a full snapshot of all 23 bot scores at decision time, and every
closed position is joined back to it. That join is the entire basis of the
learning loop: without the snapshot there is no way to ask, a year later,
which bot actually saw this coming.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any, Iterator

from sqlalchemy import (
    Boolean, Column, Date, DateTime, Float, ForeignKey, Index, Integer,
    MetaData, String, Table, Text, UniqueConstraint, create_engine, select,
)
from sqlalchemy.engine import Engine

from src.config import database_url

log = logging.getLogger(__name__)

metadata = MetaData()


# --- Reference data ---------------------------------------------------------

stocks = Table(
    "stocks", metadata,
    Column("symbol", String(32), primary_key=True),
    Column("name", String(255)),
    Column("sector", String(128)),
    Column("industry", String(128)),
    Column("isin", String(32)),
    Column("in_universe", Boolean, default=True),
    Column("updated_at", DateTime, default=datetime.now),
)

# Daily bars including delivery, which Yahoo does not carry. The daily scan
# appends here so that delivery history accumulates over time rather than
# needing one bhavcopy request per session on every run.
price_bars = Table(
    "price_bars", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("symbol", String(32), nullable=False),
    Column("date", Date, nullable=False),
    Column("open", Float), Column("high", Float),
    Column("low", Float), Column("close", Float),
    Column("volume", Float),
    Column("deliverable_qty", Float),
    Column("delivery_pct", Float),
    Column("turnover_lacs", Float),
    UniqueConstraint("symbol", "date", name="uq_price_bar"),
    Index("ix_price_bars_symbol_date", "symbol", "date"),
)


# --- Screening --------------------------------------------------------------

scans = Table(
    "scans", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_at", DateTime, default=datetime.now, nullable=False),
    Column("trade_date", Date),
    Column("universe", String(64)),
    Column("universe_size", Integer),
    Column("passed_quality", Integer),
    Column("passed_dip", Integer),
    Column("passed_delivery", Integer),
    Column("status", String(32), default="running"),  # running | complete | failed
    Column("error", Text),
    Column("duration_seconds", Float),
)

candidates = Table(
    "candidates", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("scan_id", Integer, ForeignKey("scans.id"), nullable=False),
    Column("symbol", String(32), nullable=False),
    Column("rank", Integer),
    Column("screen_score", Float),
    Column("close", Float),
    Column("rsi", Float),
    Column("drawdown_pct", Float),
    Column("dma_200", Float),
    Column("dma_slope_pct", Float),
    Column("atr", Float),
    Column("delivery_ratio", Float),
    Column("delivery_persistence", Integer),
    # Full metric payload, so a candidate can be re-examined later without
    # refetching data that may no longer be available in the same form.
    Column("metrics_json", Text),
    Column("stage_failures_json", Text),
    Index("ix_candidates_scan", "scan_id"),
)


# --- Committee --------------------------------------------------------------

committee_runs = Table(
    "committee_runs", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("symbol", String(32), nullable=False),
    Column("run_at", DateTime, default=datetime.now, nullable=False),
    Column("trade_date", Date),
    Column("price_at_run", Float),
    Column("stance", String(32)),           # BUY | WATCH | NO_BUY
    Column("conviction", Float),            # 0-100
    Column("recommendation", String(32)),   # AGGRESSIVE | BALANCED | CONSERVATIVE | NO_BUY
    Column("forensics_veto", Boolean, default=False),
    Column("bull_bear_agree", Boolean),
    # Sizing output and exit doctrine, stored whole so the report can be
    # rebuilt exactly as it was issued.
    Column("sizing_json", Text),
    Column("exit_doctrine_json", Text),
    Column("summary", Text),
    Column("dissent", Text),
    # The weight set in force, so attribution can compare across versions
    Column("weights_version", Integer),
    Column("cost_usd", Float),
    Column("cache_read_tokens", Integer),
    Column("input_tokens", Integer),
    Column("output_tokens", Integer),
    Column("duration_seconds", Float),
    Index("ix_committee_symbol_date", "symbol", "run_at"),
)

# One row per bot per run. This is the learning loop's training data: the
# score each bot gave before the outcome was known.
bot_verdicts = Table(
    "bot_verdicts", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", Integer, ForeignKey("committee_runs.id"), nullable=False),
    Column("bot_id", String(64), nullable=False),
    Column("desk", String(32)),
    Column("role", String(32)),             # analyst | lead | cmio
    Column("score", Float),                 # -5 .. +5
    Column("confidence", Float),            # 0 .. 1
    Column("stance", String(32)),
    Column("data_available", Boolean, default=True),
    Column("key_findings_json", Text),
    Column("evidence_json", Text),
    Column("red_flags_json", Text),
    Column("raw_json", Text),
    UniqueConstraint("run_id", "bot_id", name="uq_bot_verdict"),
    Index("ix_bot_verdicts_bot", "bot_id"),
)


# --- Portfolio --------------------------------------------------------------

positions = Table(
    "positions", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("symbol", String(32), nullable=False),
    Column("committee_run_id", Integer, ForeignKey("committee_runs.id")),
    Column("status", String(16), default="open"),   # open | closed
    Column("recommendation", String(32)),
    Column("conviction", Float),
    Column("entry_date", Date),
    Column("avg_entry_price", Float),
    Column("quantity", Float, default=0.0),
    Column("planned_value", Float),
    Column("invested_value", Float),
    Column("stop_price", Float),
    Column("tranches_json", Text),
    Column("exit_doctrine_json", Text),
    # Highest close since entry, for the trailing stop and for the
    # "how much more could we have made" post-mortem question.
    Column("peak_price", Float),
    Column("peak_date", Date),
    Column("exit_date", Date),
    Column("exit_price", Float),
    Column("exit_reason", String(128)),
    Column("realised_pnl", Float),
    Column("notes", Text),
    Index("ix_positions_status", "status"),
)

# Every buy and sell, as it happened. This is the source of truth for the
# portfolio: quantity, average cost and status on the positions row above are
# all derived from these rows and recomputed whenever one changes.
#
# The strategy enters in three tranches and leaves in trims, so a position is
# never one price on one date. Storing an average would lose the individual
# lots - and with them the per-lot holding period that Indian FIFO tax rules
# require.
transactions = Table(
    "transactions", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("position_id", Integer, ForeignKey("positions.id"), index=True),
    Column("symbol", String(32), nullable=False),
    Column("side", String(4), nullable=False),        # BUY | SELL
    Column("trade_date", Date, nullable=False),
    Column("quantity", Float, nullable=False),
    Column("price", Float, nullable=False),

    # Itemised the way a contract note itemises them.
    Column("brokerage", Float, default=0.0),
    Column("stt", Float, default=0.0),
    Column("exchange_fee", Float, default=0.0),
    Column("sebi_fee", Float, default=0.0),
    Column("stamp_duty", Float, default=0.0),
    Column("gst", Float, default=0.0),
    Column("dp_charge", Float, default=0.0),
    Column("other_charges", Float, default=0.0),
    Column("total_charges", Float, default=0.0),

    Column("gross_amount", Float),   # quantity * price
    Column("net_amount", Float),     # what actually moved in or out of the bank

    Column("broker", String(32)),
    Column("order_id", String(64)),
    Column("exchange", String(8)),
    Column("tranche_label", String(32)),   # T1 | T2 | T3 | trim | exit
    Column("notes", Text),
    Column("source", String(16), default="manual"),   # manual | import
    Column("created_at", DateTime, default=datetime.now),

    # Re-importing the same broker file must be a no-op rather than a
    # duplicate. Where a file carries no order id the importer synthesises a
    # stable hash instead, so this constraint always has something to bite on.
    UniqueConstraint("broker", "order_id", name="uq_transaction_order"),
    Index("ix_transactions_symbol_date", "symbol", "trade_date"),
)

# Superseded by `transactions`, which records the same events with the cost
# detail and lot identity this table lacked. Kept so existing databases still
# open; nothing writes to it.
position_events = Table(
    "position_events", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("position_id", Integer, ForeignKey("positions.id"), nullable=False),
    Column("occurred_at", DateTime, default=datetime.now),
    Column("event_type", String(32)),   # tranche_fill | trim | exit | review | note
    Column("price", Float),
    Column("quantity", Float),
    Column("detail", Text),
)

exit_signals = Table(
    "exit_signals", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("position_id", Integer, ForeignKey("positions.id"), nullable=False),
    Column("signalled_at", DateTime, default=datetime.now),
    Column("rule", String(64)),         # thesis_break | trailing_stop | target | ltcg | valuation
    Column("severity", String(16)),     # info | warn | urgent
    Column("action", String(32)),       # HOLD | TRIM | EXIT | REVIEW
    Column("detail", Text),
    Column("acknowledged", Boolean, default=False),
    Index("ix_exit_signals_position", "position_id"),
)


# --- Learning ---------------------------------------------------------------

# The closed-trade ledger. Denormalised on purpose: attribution runs over
# this table constantly, and it must stay readable even if a position row is
# later edited by hand.
trades = Table(
    "trades", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("position_id", Integer, ForeignKey("positions.id")),
    Column("committee_run_id", Integer, ForeignKey("committee_runs.id")),
    Column("symbol", String(32), nullable=False),
    Column("entry_date", Date), Column("exit_date", Date),
    Column("entry_price", Float), Column("exit_price", Float),
    Column("quantity", Float),
    Column("return_pct", Float),
    Column("holding_days", Integer),
    Column("is_win", Boolean),
    Column("conviction", Float),
    Column("conviction_band", String(32)),
    Column("recommendation", String(32)),
    # Maximum favourable excursion: the best price reached while held. The
    # gap between this and the exit is the "how could we have won more"
    # question, and it cannot be reconstructed after the fact.
    Column("mfe_pct", Float),
    Column("mae_pct", Float),            # worst excursion, for stop calibration
    Column("captured_fraction", Float),  # return_pct / mfe_pct
    Column("exit_reason", String(128)),
    Column("was_ltcg", Boolean),
    Column("closed_at", DateTime, default=datetime.now),
    Index("ix_trades_symbol", "symbol"),
)

# Forward returns for open positions at fixed checkpoints, so attribution has
# something to learn from long before a long-term position ever closes.
trade_checkpoints = Table(
    "trade_checkpoints", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("committee_run_id", Integer, ForeignKey("committee_runs.id"), nullable=False),
    Column("symbol", String(32), nullable=False),
    Column("horizon_days", Integer, nullable=False),
    Column("measured_at", Date),
    Column("forward_return_pct", Float),
    Column("benchmark_return_pct", Float),
    Column("excess_return_pct", Float),
    UniqueConstraint("committee_run_id", "horizon_days", name="uq_checkpoint"),
)

bot_attribution = Table(
    "bot_attribution", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("computed_at", DateTime, default=datetime.now),
    Column("bot_id", String(64), nullable=False),
    Column("desk", String(32)),
    Column("horizon_days", Integer),
    Column("n_observations", Integer),
    Column("information_coefficient", Float),  # Spearman rank correlation
    Column("ic_p_value", Float),
    Column("hit_rate", Float),
    Column("avg_score_on_wins", Float),
    Column("avg_score_on_losses", Float),
    Column("verdict", String(32)),   # predictive | noise | misleading
    Index("ix_attribution_bot", "bot_id", "computed_at"),
)

# Versioned committee weights. Every change is a new row, so the dashboard
# can show performance by weight version and roll back exactly.
weight_versions = Table(
    "weight_versions", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("created_at", DateTime, default=datetime.now),
    Column("is_active", Boolean, default=False),
    Column("source", String(32)),        # seed | learner | manual | rollback
    Column("payload_json", Text, nullable=False),
    Column("n_trades_at_update", Integer),
    Column("in_sample_score", Float),
    Column("out_of_sample_score", Float),
    Column("passed_oos_gate", Boolean),
    Column("rationale", Text),
    Column("rolled_back_from", Integer),
)

# Narrative lessons injected into the CMIO prompt as institutional memory.
lessons = Table(
    "lessons", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("created_at", DateTime, default=datetime.now),
    Column("trade_id", Integer, ForeignKey("trades.id")),
    Column("symbol", String(32)),
    Column("outcome", String(16)),       # win | loss
    Column("category", String(64)),      # thesis_break | sizing | timing | data_gap | exit
    Column("lesson", Text, nullable=False),
    Column("evidence", Text),
    Column("confidence", Float),
    Column("times_applied", Integer, default=0),
    Column("is_active", Boolean, default=True),
)

# Proposed threshold changes. Weights auto-apply; thresholds never do,
# because a threshold change alters what the strategy fundamentally is.
proposals = Table(
    "proposals", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("created_at", DateTime, default=datetime.now),
    Column("kind", String(32)),          # threshold | weight | rule
    Column("config_path", String(128)),
    Column("current_value", String(64)),
    Column("proposed_value", String(64)),
    Column("rationale", Text),
    Column("evidence_json", Text),
    Column("status", String(16), default="pending"),  # pending | approved | rejected
    Column("decided_at", DateTime),
)


# --- Alerts -----------------------------------------------------------------

alerts_sent = Table(
    "alerts_sent", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("sent_at", DateTime, default=datetime.now),
    Column("channel", String(32)),
    Column("alert_type", String(32)),
    Column("symbol", String(32)),
    # Dedupe key, so a re-run of the daily scan cannot send the same alert
    # twice. Unique, which makes the database itself the guard.
    Column("dedupe_key", String(255), unique=True),
    Column("body", Text),
    Column("delivered", Boolean, default=False),
    Column("error", Text),
)


# --- Engine -----------------------------------------------------------------

_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        url = database_url()
        kwargs: dict[str, Any] = {"future": True}
        if url.startswith("sqlite"):
            # Allow use from Streamlit's worker threads.
            kwargs["connect_args"] = {"check_same_thread": False}
        else:
            # Hosted Postgres closes idle connections; pre-ping avoids
            # handing out a dead one after the app has been asleep. Neon in
            # particular scales to zero, so the first query after a quiet
            # spell would otherwise hit a closed socket.
            kwargs.update({"pool_pre_ping": True, "pool_recycle": 300})

            # Hosted Postgres requires TLS. config.database_url() strips
            # libpq's ?sslmode=require because pg8000 does not understand it;
            # this is where the equivalent is actually applied.
            if "pg8000" in url:
                import ssl

                context = ssl.create_default_context()
                kwargs["connect_args"] = {"ssl_context": context}
        _engine = create_engine(url, **kwargs)
        log.debug("DB engine created for %s", url.split("@")[-1])
    return _engine


def init_db(drop: bool = False) -> None:
    engine = get_engine()
    if drop:
        metadata.drop_all(engine)
    metadata.create_all(engine)

    if engine.url.get_backend_name() == "sqlite":
        with engine.begin() as conn:
            from sqlalchemy import text
            # WAL lets the dashboard read while the scan job writes.
            conn.execute(text("PRAGMA journal_mode=WAL"))
            conn.execute(text("PRAGMA synchronous=NORMAL"))


@contextmanager
def connection() -> Iterator[Any]:
    """Transactional scope. Commits on success, rolls back on error."""
    with get_engine().begin() as conn:
        yield conn


def to_json(value: Any) -> str | None:
    """Serialise for a Text column, tolerating dates and numpy scalars."""
    if value is None:
        return None

    def default(obj: Any) -> Any:
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        if hasattr(obj, "item"):       # numpy scalar
            return obj.item()
        if hasattr(obj, "to_dict"):    # pandas object
            return obj.to_dict()
        return str(obj)

    return json.dumps(value, default=default)


def from_json(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def active_weights() -> dict[str, Any] | None:
    """The weight set currently in force, or None before the first seed."""
    with connection() as conn:
        row = conn.execute(
            select(weight_versions.c.id, weight_versions.c.payload_json)
            .where(weight_versions.c.is_active.is_(True))
            .order_by(weight_versions.c.created_at.desc())
            .limit(1)
        ).first()
    if row is None:
        return None
    payload = from_json(row.payload_json, {})
    payload["_version"] = row.id
    return payload
