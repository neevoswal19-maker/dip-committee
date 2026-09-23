"""The data contract every fetcher honours.

Two ideas carry through the whole system:

`DataResult` wraps every fetch so that "we could not get this" travels as
data rather than as an exception or a silent None. When the Insider Activity
Analyst has no disclosures, the difference between "there were none" and "NSE
was down" changes the verdict completely, and only an explicit wrapper keeps
those apart.

`MarketDataProvider` is the seam for swapping data sources. Today everything
comes from free scrapers. When a paid key arrives, a new subclass drops in
without a single change to the screener, the bots or the dashboard.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Generic, TypeVar

import pandas as pd

T = TypeVar("T")


class DataStatus(str, Enum):
    OK = "ok"
    STALE = "stale"           # served from cache past its TTL
    PARTIAL = "partial"       # some of what was asked for
    UNAVAILABLE = "unavailable"  # fetch failed
    NOT_APPLICABLE = "not_applicable"  # genuinely nothing to report


@dataclass
class DataResult(Generic[T]):
    """A fetch outcome, its provenance and its age.

    `ok` is deliberately narrow: it means we have data we trust. Stale data
    is usable but flagged, so the Research Validation Analyst can dock
    confidence instead of the committee quietly reasoning over last month's
    shareholding pattern as though it were current.
    """

    value: T | None = None
    status: DataStatus = DataStatus.OK
    source: str = "unknown"
    fetched_at: datetime = field(default_factory=datetime.now)
    as_of: date | None = None
    note: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is DataStatus.OK and self.value is not None

    @property
    def usable(self) -> bool:
        """Present enough to reason over, even if imperfect."""
        return self.value is not None and self.status in (
            DataStatus.OK,
            DataStatus.STALE,
            DataStatus.PARTIAL,
        )

    @property
    def available(self) -> bool:
        """What a bot reports as `data_available` in its verdict."""
        return self.status is not DataStatus.UNAVAILABLE

    def unwrap(self, default: Any = None) -> Any:
        return self.value if self.value is not None else default

    def age_days(self) -> int | None:
        if self.as_of is None:
            return None
        return (date.today() - self.as_of).days

    def describe(self) -> str:
        """One line for the evidence pack, so bots can see data provenance."""
        bits = [f"source={self.source}", f"status={self.status.value}"]
        if self.as_of:
            bits.append(f"as_of={self.as_of.isoformat()}")
            age = self.age_days()
            if age is not None and age > 0:
                bits.append(f"age={age}d")
        if self.note:
            bits.append(f"note={self.note}")
        return ", ".join(bits)

    @classmethod
    def unavailable(cls, source: str, note: str | None = None) -> DataResult[T]:
        return cls(value=None, status=DataStatus.UNAVAILABLE, source=source, note=note)

    @classmethod
    def empty(cls, source: str, note: str | None = None) -> DataResult[T]:
        """Fetched successfully, and the honest answer is 'nothing to report'."""
        return cls(value=None, status=DataStatus.NOT_APPLICABLE, source=source, note=note)


@dataclass
class StockIdentity:
    """A symbol in the several forms the different sources insist on."""

    symbol: str                      # NSE trading symbol, e.g. RELIANCE
    name: str | None = None
    sector: str | None = None
    industry: str | None = None
    isin: str | None = None

    @property
    def yahoo(self) -> str:
        """Yahoo Finance wants an .NS suffix for NSE-listed equities."""
        return f"{self.symbol}.NS"

    def __str__(self) -> str:
        return self.symbol


class MarketDataProvider(ABC):
    """Everything the screener and the committee need from the outside world.

    Implementations must never raise for an upstream failure - return a
    DataResult with UNAVAILABLE instead. A scan over 500 stocks cannot be
    allowed to die because one endpoint returned a 403.
    """

    name: str = "abstract"

    # --- Universe ---
    @abstractmethod
    def get_universe(self, index: str) -> DataResult[list[StockIdentity]]:
        """Constituents of an index, e.g. 'NIFTY 500'."""

    # --- Prices ---
    @abstractmethod
    def get_price_history(self, stock: StockIdentity, years: float = 6.0) -> DataResult[pd.DataFrame]:
        """Daily OHLCV, lowercase columns, DatetimeIndex ascending."""

    # --- Delivery (NSE-specific, the Stage 3 edge) ---
    @abstractmethod
    def get_delivery_history(self, stock: StockIdentity, days: int = 120) -> DataResult[pd.DataFrame]:
        """Daily deliverable quantity and traded quantity."""

    # --- Fundamentals ---
    @abstractmethod
    def get_fundamentals(self, stock: StockIdentity) -> DataResult[dict[str, Any]]:
        """Valuation, growth, returns and balance-sheet ratios."""

    @abstractmethod
    def get_financial_statements(self, stock: StockIdentity) -> DataResult[dict[str, pd.DataFrame]]:
        """Income statement, balance sheet and cash flow, for the forensics desk."""

    # --- Ownership ---
    @abstractmethod
    def get_shareholding_pattern(self, stock: StockIdentity) -> DataResult[pd.DataFrame]:
        """Quarterly promoter / FII / DII / MF / public holding percentages."""

    @abstractmethod
    def get_insider_trades(self, stock: StockIdentity, days: int = 90) -> DataResult[pd.DataFrame]:
        """SEBI PIT and SAST disclosures - who acquired or disposed, and how much."""

    @abstractmethod
    def get_bulk_block_deals(self, stock: StockIdentity, days: int = 90) -> DataResult[pd.DataFrame]:
        """Bulk and block deals, which explain one-off delivery spikes."""

    # --- Market activity ---
    @abstractmethod
    def get_short_selling(self, stock: StockIdentity, days: int = 30) -> DataResult[pd.DataFrame]:
        """Daily short-selling quantities reported by NSE."""

    @abstractmethod
    def get_surveillance_flags(self, stock: StockIdentity) -> DataResult[dict[str, Any]]:
        """ASM / GSM / F&O ban membership. A quality-gate disqualifier."""

    # --- News ---
    @abstractmethod
    def get_news(self, stock: StockIdentity, days: int = 30) -> DataResult[list[dict[str, Any]]]:
        """Recent headlines with source, timestamp and URL."""

    @abstractmethod
    def get_corporate_announcements(self, stock: StockIdentity, days: int = 90) -> DataResult[pd.DataFrame]:
        """NSE filings: results, board meetings, ratings, pledges."""

    # --- Macro ---
    @abstractmethod
    def get_index_history(self, index_symbol: str, years: float = 2.0) -> DataResult[pd.DataFrame]:
        """Index or sector index history, for relative strength."""

    @abstractmethod
    def get_macro_series(self) -> DataResult[dict[str, pd.DataFrame]]:
        """Crude, USDINR, India VIX - inputs for the geopolitical desk."""
