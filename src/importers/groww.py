"""Reading a broker trade file, Groww first but not Groww only.

Groww exports order history as XLSX and transactions as CSV, and its exact
column headers are not publicly documented and change between report types.
So nothing here depends on knowing them: headers are matched against alias
lists, the detected mapping is handed back for you to correct in the UI, and
nothing is written until you confirm.

Two wrinkles specific to Indian broker files:

**Names, not symbols.** A Groww row says "Reliance Industries Ltd" while the
rest of this system speaks in NSE symbols (`RELIANCE`). Resolution goes
through the NSE equity master, and anything that cannot be matched with
confidence is surfaced for manual mapping rather than guessed at or dropped.

**Re-imports.** Exports overlap - you download a year, then download it again
next month. Every row gets a stable identity, from the broker's order id where
there is one and from a hash of the trade's own details where there is not, so
importing the same file twice adds nothing.
"""

from __future__ import annotations

import difflib
import io
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, BinaryIO

import pandas as pd

from src import db, portfolio
from src.data.provider import StockIdentity

log = logging.getLogger(__name__)

#: Header aliases per field, lower-cased and space-collapsed before matching.
#: Ordered roughly by how specific they are.
ALIASES: dict[str, tuple[str, ...]] = {
    "symbol": (
        "symbol", "trading symbol", "tradingsymbol", "scrip", "scrip name",
        "stock name", "stock", "company name", "company", "instrument",
        "security name", "name",
    ),
    "side": (
        "side", "type", "trade type", "order type", "transaction type",
        "buy/sell", "buy or sell", "b/s", "transaction",
    ),
    "quantity": (
        "quantity", "qty", "shares", "filled qty", "executed quantity",
        "traded qty", "no of shares", "units",
    ),
    "price": (
        "price", "avg price", "average price", "trade price", "executed price",
        "buy price", "sell price", "rate", "avg. price", "price per share",
    ),
    "trade_date": (
        "trade date", "date", "order date", "execution date", "transaction date",
        "order execution time", "executed at", "timestamp", "date & time",
    ),
    "order_id": (
        "order id", "order no", "order number", "trade id", "reference",
        "reference number", "exchange order id", "nse order id",
    ),
    "charges": (
        "charges", "total charges", "taxes", "taxes and charges",
        "brokerage and charges", "total taxes", "fees",
    ),
    "exchange": ("exchange", "exch", "segment"),
}

REQUIRED = ("symbol", "side", "quantity", "price", "trade_date")

BUY_WORDS = ("buy", "b", "bought", "purchase", "credit", "debit to demat")
SELL_WORDS = ("sell", "s", "sold", "sale", "credit from demat")

#: Suffixes on Indian company names that carry no identifying information.
NAME_NOISE = re.compile(
    r"\b(ltd|limited|pvt|private|india|indian|corporation|corp|company|co|"
    r"industries|enterprises|the)\b",
    re.IGNORECASE,
)


@dataclass
class ImportRow:
    """One parsed trade, with whatever went wrong attached to it."""

    row_number: int
    symbol: str | None = None
    raw_symbol: str = ""
    side: str | None = None
    quantity: float | None = None
    price: float | None = None
    trade_date: date | None = None
    order_id: str | None = None
    charges: float | None = None
    exchange: str = "NSE"
    problems: list[str] = field(default_factory=list)
    duplicate: bool = False

    @property
    def ok(self) -> bool:
        return not self.problems and not self.duplicate

    def to_display(self) -> dict[str, Any]:
        return {
            "Row": self.row_number,
            "Symbol": self.symbol or f"? {self.raw_symbol}"[:28],
            "Side": self.side or "?",
            "Qty": self.quantity,
            "Price": self.price,
            "Date": self.trade_date.isoformat() if self.trade_date else None,
            "Charges": self.charges,
            "Status": (
                "already imported" if self.duplicate
                else ("ready" if self.ok else "; ".join(self.problems)[:60])
            ),
        }


@dataclass
class ImportPreview:
    rows: list[ImportRow]
    mapping: dict[str, str | None]
    columns: list[str]
    detected_broker: str = "groww"

    @property
    def ready(self) -> list[ImportRow]:
        return [r for r in self.rows if r.ok]

    @property
    def duplicates(self) -> list[ImportRow]:
        return [r for r in self.rows if r.duplicate]

    @property
    def problems(self) -> list[ImportRow]:
        return [r for r in self.rows if r.problems and not r.duplicate]

    @property
    def unresolved_names(self) -> list[str]:
        seen: list[str] = []
        for row in self.rows:
            if row.symbol is None and row.raw_symbol and row.raw_symbol not in seen:
                seen.append(row.raw_symbol)
        return seen


# --- Reading the file -------------------------------------------------------


def read_file(data: bytes | BinaryIO, filename: str) -> pd.DataFrame:
    """Load a CSV or XLSX into a frame, tolerating a preamble.

    Broker exports frequently open with a title row, an account number and a
    blank line before the real header. If the first parse produces unnamed
    columns, the file is re-read from the row that looks like the header.
    """
    raw = data if isinstance(data, bytes) else data.read()
    lower = filename.lower()

    def load(skip: int) -> pd.DataFrame:
        if lower.endswith((".xlsx", ".xls")):
            return pd.read_excel(io.BytesIO(raw), skiprows=skip)
        return pd.read_csv(io.BytesIO(raw), skiprows=skip)

    # A preamble does not merely produce odd headers - a one-column title row
    # above a seven-column table makes pandas raise outright, so the first
    # attempt has to be allowed to fail before the search begins.
    try:
        frame = load(0)
        if not _looks_like_preamble(frame):
            return frame
    except Exception as exc:
        log.debug("%s did not parse from row 0 (%s); looking for the header", filename, exc)
        frame = None

    for skip in range(1, 12):
        try:
            candidate = load(skip)
        except Exception:
            continue
        if not _looks_like_preamble(candidate) and len(candidate.columns) > 2:
            log.info("Skipped %d preamble rows in %s", skip, filename)
            return candidate

    if frame is None:
        raise ValueError(
            f"Could not find a header row in {filename}. Check it is a trade export "
            f"rather than a summary or statement."
        )
    return frame


def _looks_like_preamble(frame: pd.DataFrame) -> bool:
    if frame is None or frame.empty or len(frame.columns) < 3:
        return True
    unnamed = sum(1 for c in frame.columns if str(c).lower().startswith("unnamed"))
    return unnamed > len(frame.columns) / 2


def _normalise(header: Any) -> str:
    return re.sub(r"[^a-z0-9/ ]+", " ", str(header).strip().lower())


def detect_mapping(frame: pd.DataFrame) -> dict[str, str | None]:
    """Guess which column holds which field.

    Exact alias match first, then fuzzy, and a column is never assigned to two
    fields. The result is a starting point for the UI to correct, not a
    verdict - which is why a wrong guess here is recoverable.
    """
    normalised = {column: _normalise(column) for column in frame.columns}
    mapping: dict[str, str | None] = {}
    claimed: set[str] = set()

    for field_name, aliases in ALIASES.items():
        chosen = None
        for alias in aliases:
            for column, text in normalised.items():
                if column in claimed:
                    continue
                if text == alias:
                    chosen = column
                    break
            if chosen:
                break

        if chosen is None:
            for alias in aliases:
                for column, text in normalised.items():
                    if column in claimed:
                        continue
                    if alias in text or text in alias:
                        chosen = column
                        break
                if chosen:
                    break

        if chosen is None:
            available = {c: t for c, t in normalised.items() if c not in claimed}
            for alias in aliases:
                close = difflib.get_close_matches(alias, list(available.values()), n=1, cutoff=0.85)
                if close:
                    chosen = next(c for c, t in available.items() if t == close[0])
                    break

        if chosen:
            claimed.add(chosen)
        mapping[field_name] = chosen

    return mapping


# --- Parsing values ---------------------------------------------------------


def _parse_side(value: Any) -> str | None:
    text = str(value).strip().lower()
    if not text or text in ("nan", "none"):
        return None
    for word in SELL_WORDS:
        if text == word or text.startswith(word):
            return "SELL"
    for word in BUY_WORDS:
        if text == word or text.startswith(word):
            return "BUY"
    if "sell" in text or "sold" in text:
        return "SELL"
    if "buy" in text or "bought" in text:
        return "BUY"
    return None


def _parse_number(value: Any) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = re.sub(r"[^0-9.\-]", "", str(value))
    if not text or text in ("-", "."):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _parse_date(value: Any) -> date | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = str(value).strip()
    if not text or text.lower() in ("nan", "nat"):
        return None

    # dayfirst, because Indian broker files are overwhelmingly DD-MM-YYYY and
    # pandas would otherwise read 03-04-2026 as 3 April or 4 March by luck.
    try:
        parsed = pd.to_datetime(text, dayfirst=True, errors="coerce")
        return None if pd.isna(parsed) else parsed.date()
    except Exception:
        return None


# --- Resolving company names to NSE symbols ---------------------------------


def _clean_name(name: str) -> str:
    return re.sub(r"\s+", " ", NAME_NOISE.sub(" ", str(name))).strip().lower()


def build_symbol_index() -> dict[str, str]:
    """Map cleaned company names and symbols to NSE trading symbols.

    Built from the local stocks table first, topped up from the NSE equity
    master. Failure to reach NSE degrades to whatever is already known rather
    than aborting the import.
    """
    index: dict[str, str] = {}

    try:
        from sqlalchemy import select

        with db.connection() as conn:
            for row in conn.execute(select(db.stocks.c.symbol, db.stocks.c.name)):
                index[row.symbol.lower()] = row.symbol
                if row.name:
                    index[_clean_name(row.name)] = row.symbol
    except Exception as exc:
        log.debug("Could not read the stocks table: %s", exc)

    try:
        from src.data import nse

        master = nse.get_equity_master()
        if master.usable and master.value is not None:
            for _, row in master.value.iterrows():
                symbol = str(row.get("SYMBOL", "")).strip().upper()
                name = str(row.get("NAME OF COMPANY", "")).strip()
                if symbol:
                    index.setdefault(symbol.lower(), symbol)
                    if name:
                        index.setdefault(_clean_name(name), symbol)
    except Exception as exc:
        log.debug("Could not load the NSE equity master: %s", exc)

    return index


def resolve_symbol(raw: str, index: dict[str, str]) -> str | None:
    """Turn whatever the broker wrote into an NSE symbol, or admit defeat.

    Returns None rather than a best guess when nothing matches confidently.
    A wrong symbol silently attaches trades to the wrong company, which is
    far worse than asking.
    """
    if not raw:
        return None

    text = str(raw).strip()
    if not text:
        return None

    direct = text.upper().replace(".NS", "").replace("-EQ", "")
    if direct.lower() in index:
        return index[direct.lower()]

    cleaned = _clean_name(text)
    if cleaned in index:
        return index[cleaned]

    close = difflib.get_close_matches(cleaned, list(index.keys()), n=1, cutoff=0.88)
    if close:
        return index[close[0]]

    return None


# --- Building the preview ---------------------------------------------------


def existing_order_ids(broker: str) -> set[str]:
    from sqlalchemy import select

    with db.connection() as conn:
        rows = conn.execute(
            select(db.transactions.c.order_id).where(db.transactions.c.broker == broker)
        ).fetchall()
    return {r.order_id for r in rows if r.order_id}


def build_preview(
    frame: pd.DataFrame,
    *,
    mapping: dict[str, str | None] | None = None,
    broker: str = "groww",
    symbol_overrides: dict[str, str] | None = None,
) -> ImportPreview:
    """Parse and validate every row without writing anything."""
    mapping = mapping or detect_mapping(frame)
    overrides = {k.lower(): v for k, v in (symbol_overrides or {}).items()}

    index = build_symbol_index()
    seen_ids = existing_order_ids(broker)
    within_file: set[str] = set()

    rows: list[ImportRow] = []

    for position, (_, record) in enumerate(frame.iterrows(), start=1):
        row = ImportRow(row_number=position)

        def value(field_name: str) -> Any:
            column = mapping.get(field_name)
            return record.get(column) if column else None

        row.raw_symbol = str(value("symbol") or "").strip()
        row.symbol = (
            overrides.get(row.raw_symbol.lower())
            or resolve_symbol(row.raw_symbol, index)
        )
        row.side = _parse_side(value("side"))
        row.quantity = _parse_number(value("quantity"))
        row.price = _parse_number(value("price"))
        row.trade_date = _parse_date(value("trade_date"))
        row.charges = _parse_number(value("charges"))

        order_id = value("order_id")
        row.order_id = str(order_id).strip() if order_id is not None and str(order_id).strip() not in ("", "nan") else None

        exchange = value("exchange")
        if exchange and str(exchange).strip().lower() not in ("nan", ""):
            row.exchange = str(exchange).strip().upper()[:8]

        if not row.raw_symbol:
            row.problems.append("no symbol")
        elif row.symbol is None:
            row.problems.append(f"could not match '{row.raw_symbol[:24]}' to an NSE symbol")
        if row.side is None:
            row.problems.append("side is not recognisable as buy or sell")
        if row.quantity is None or row.quantity <= 0:
            row.problems.append("quantity missing or not positive")
        if row.price is None or row.price <= 0:
            row.problems.append("price missing or not positive")
        if row.trade_date is None:
            row.problems.append("date could not be read")
        elif row.trade_date > date.today():
            row.problems.append("date is in the future")

        if not row.problems:
            identity = row.order_id or portfolio.synthetic_order_id(
                row.symbol, row.side, row.trade_date, row.quantity, row.price
            )
            row.order_id = identity
            if identity in seen_ids or identity in within_file:
                row.duplicate = True
            within_file.add(identity)

        rows.append(row)

    return ImportPreview(
        rows=rows, mapping=mapping, columns=list(frame.columns), detected_broker=broker
    )


def commit(
    preview: ImportPreview,
    *,
    broker: str = "groww",
    cfg: Any = None,
    estimate_missing_charges: bool = True,
) -> dict[str, Any]:
    """Write the importable rows.

    Applied oldest first, so FIFO sees purchases before the sales that
    consume them. A row that fails is recorded and skipped rather than
    aborting the batch - a single odd trade should not block the other fifty.
    """
    from src import charges as charge_model

    ordered = sorted(
        preview.ready, key=lambda r: (r.trade_date or date.min, r.row_number)
    )

    imported = 0
    failures: list[dict[str, Any]] = []

    for row in ordered:
        try:
            if row.charges is not None and row.charges > 0:
                cost = {"total": round(float(row.charges), 2), "other": round(float(row.charges), 2)}
            elif estimate_missing_charges:
                cost = charge_model.estimate(
                    row.side, row.quantity, row.price, cfg=cfg, broker=broker
                ).to_dict()
            else:
                cost = {"total": 0.0}

            portfolio.record_trade(
                row.symbol, row.side, row.trade_date, row.quantity, row.price,
                charges=cost, broker=broker, order_id=row.order_id,
                exchange=row.exchange, source="import", cfg=cfg,
            )
            imported += 1
        except Exception as exc:
            failures.append({"row": row.row_number, "symbol": row.symbol, "error": str(exc)})
            log.warning("Import row %s (%s) failed: %s", row.row_number, row.symbol, exc)

    return {
        "imported": imported,
        "skipped_duplicates": len(preview.duplicates),
        "skipped_problems": len(preview.problems),
        "failures": failures,
    }
