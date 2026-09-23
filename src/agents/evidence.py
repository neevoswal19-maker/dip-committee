"""The evidence pack: everything the committee sees, fetched once.

Twenty-three bots reading the same stock must not each trigger their own
round of NSE and Yahoo requests. The pack is built once, and every field stays
wrapped in its `DataResult` so a bot can tell "there were no insider trades"
from "NSE was unreachable" - a distinction that changes the verdict.

Nothing here raises. A field that could not be fetched is simply absent, the
bots that needed it report themselves blind, and the Research Validation
Analyst totals up who was flying without instruments.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pandas as pd

from src import indicators as ind
from src.config import load_config
from src.data import fundamentals as fnd
from src.data import nse, prices
from src.data.provider import DataResult, StockIdentity

log = logging.getLogger(__name__)


@dataclass
class EvidenceContext:
    """One stock's evidence, as the committee receives it."""

    stock: StockIdentity
    as_of: date = field(default_factory=date.today)
    cfg: Any = None

    price_frame: pd.DataFrame | None = None
    delivery: DataResult | None = None
    fundamentals: DataResult | None = None
    shareholding: DataResult | None = None
    insider: DataResult | None = None
    announcements: DataResult | None = None
    news: DataResult | None = None
    deals: DataResult | None = None
    surveillance: DataResult | None = None
    sector_index: DataResult | None = None
    benchmark_index: DataResult | None = None
    macro: DataResult | None = None

    #: Verdicts already produced this run. The review desk reads these -
    #: Bull and Bear argue from what the other bots found, not from raw data.
    peer_verdicts: dict[str, Any] = field(default_factory=dict)

    @property
    def symbol(self) -> str:
        return self.stock.symbol

    @property
    def sector(self) -> str | None:
        if self.stock.sector:
            return self.stock.sector
        if self.fundamentals and self.fundamentals.value:
            return self.fundamentals.value.get("sector")
        return None

    @property
    def price(self) -> float:
        if self.price_frame is not None and not self.price_frame.empty:
            return float(self.price_frame["close"].iloc[-1])
        return 0.0

    @property
    def latest(self) -> pd.Series | None:
        if self.price_frame is not None and not self.price_frame.empty:
            return self.price_frame.iloc[-1]
        return None

    def reachable(self, name: str) -> bool:
        """Whether the source answered at all, even if the answer was 'nothing'.

        This is what gates a bot running. "No insider disclosures were filed"
        is a finding a bot should be allowed to report; "NSE was unreachable"
        is not. Collapsing the two would silence a bot that has something
        true and useful to say.
        """
        if name == "price_frame":
            return self.price_frame is not None and not self.price_frame.empty

        result = getattr(self, name, None)
        if not isinstance(result, DataResult):
            return result is not None
        return result.available

    def has(self, name: str) -> bool:
        """Whether a named field carries rows a bot can compute over."""
        if name == "price_frame":
            return self.price_frame is not None and not self.price_frame.empty

        result = getattr(self, name, None)
        if not isinstance(result, DataResult):
            return result is not None

        if not result.usable:
            return False
        value = result.value
        if isinstance(value, pd.DataFrame):
            return not value.empty
        if isinstance(value, (list, dict)):
            return len(value) > 0
        return value is not None

    def get(self, name: str, default: Any = None) -> Any:
        result = getattr(self, name, None)
        if isinstance(result, DataResult):
            return result.value if result.usable else default
        return result if result is not None else default

    def note_for(self, name: str) -> str:
        result = getattr(self, name, None)
        if isinstance(result, DataResult):
            return result.note or result.status.value
        return "not fetched"

    def coverage(self) -> dict[str, bool]:
        """Which sources came through. Feeds the Validation Analyst."""
        return {
            name: self.has(name)
            for name in (
                "price_frame", "delivery", "fundamentals", "shareholding",
                "insider", "announcements", "news", "deals", "surveillance",
                "sector_index", "macro",
            )
        }


def build(
    stock: StockIdentity | str,
    *,
    cfg: Any = None,
    as_of: date | None = None,
    include_news: bool = True,
    include_macro: bool = True,
) -> EvidenceContext:
    """Fetch everything the committee needs for one stock.

    Each fetch is independent and failure-tolerant: one dead endpoint costs
    that desk its evidence, not the whole run.
    """
    cfg = cfg or load_config()
    identity = StockIdentity(stock.upper()) if isinstance(stock, str) else stock
    ctx = EvidenceContext(stock=identity, as_of=as_of or date.today(), cfg=cfg)

    # --- Prices and indicators
    history = prices.get_price_history(identity, years=float(cfg.get("data.history_years", 6)))
    if history.usable and history.value is not None and not history.value.empty:
        frame = history.value
        if as_of is not None:
            frame = frame[frame.index.date <= as_of]
        if not frame.empty:
            try:
                ctx.price_frame = ind.compute_indicator_frame(
                    frame,
                    rsi_period=int(cfg.get("dip.rsi_period", 14)),
                    atr_period=int(cfg.get("dip.atr_period", 14)),
                    dma_long=int(cfg.get("dip.dma_long", 200)),
                    dma_short=int(cfg.get("dip.dma_short", 50)),
                    slope_lookback=int(cfg.get("dip.dma_slope_lookback_days", 126)),
                )
            except Exception as exc:
                log.warning("Indicator build failed for %s: %s", identity.symbol, exc)

    # --- Each of these is allowed to fail on its own
    ctx.delivery = _safe(
        lambda: nse.get_delivery_history(identity, days=int(cfg.get("delivery.avg_window_days", 20)) * 3),
        "delivery",
    )
    ctx.fundamentals = _safe(lambda: fnd.get_fundamentals(identity), "fundamentals")
    ctx.shareholding = _safe(lambda: nse.get_shareholding_pattern(identity), "shareholding")
    ctx.insider = _safe(
        lambda: nse.get_insider_trades(identity, days=int(cfg.get("ownership.insider_lookback_days", 90))),
        "insider",
    )
    ctx.announcements = _safe(lambda: nse.get_corporate_announcements(identity, days=90), "announcements")
    ctx.deals = _safe(lambda: nse.get_bulk_block_deals(identity), "deals")
    ctx.surveillance = _safe(lambda: nse.get_surveillance_flags(identity), "surveillance")

    if include_news:
        try:
            from src.data import news as news_source

            ctx.news = _safe(
                lambda: news_source.get_news(
                    identity, days=int(cfg.get("data.news_lookback_days", 30))
                ),
                "news",
            )
        except ImportError:
            ctx.news = DataResult.unavailable("news", "news module not available")

    sector = ctx.sector
    if sector:
        ctx.sector_index = _safe(lambda: prices.get_index_history(sector, years=2), "sector_index")
    ctx.benchmark_index = _safe(lambda: prices.get_index_history("nifty500", years=2), "benchmark")

    if include_macro:
        ctx.macro = _safe(lambda: prices.get_macro_series(years=2), "macro")

    return ctx


def _safe(fetch, label: str) -> DataResult:
    try:
        return fetch()
    except Exception as exc:
        log.warning("Evidence fetch failed for %s: %s", label, exc)
        return DataResult.unavailable(label, f"{type(exc).__name__}: {exc}")
