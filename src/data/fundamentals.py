"""Fundamentals and financial statements.

Yahoo's summary block covers most of the quality gate, but two things it
reports are unreliable for Indian listings - ROE is frequently null and
free cash flow is often missing - so anything the gate depends on is
recomputed from the statements rather than trusted from the summary.

The forensics metrics live here too. They are deliberately plain ratios
rather than a proprietary score: the Financial Forensics Analyst is given
the arithmetic and asked to judge it, because "cash flow has trailed
reported profit for three years" is a fact a bot can reason about, while
a blended 0-100 "quality score" is one it can only parrot.
"""

from __future__ import annotations

import logging
import warnings
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from src.data.cache import cached_fetch, make_key
from src.data.provider import DataResult, DataStatus, StockIdentity

log = logging.getLogger(__name__)

warnings.filterwarnings("ignore", category=FutureWarning, module="yfinance")

SOURCE = "yahoo"
CRORE = 1e7  # 1 crore = 10 million

#: Yahoo names statement rows inconsistently across companies, so each metric
#: lists the labels seen in the wild, tried in order of preference.
ROW_ALIASES: dict[str, tuple[str, ...]] = {
    "revenue": ("Total Revenue", "Operating Revenue"),
    "net_income": ("Net Income", "Net Income Common Stockholders", "Net Income From Continuing Operation Net Minority Interest"),
    "operating_income": ("Operating Income", "EBIT", "Total Operating Income As Reported"),
    "equity": ("Stockholders Equity", "Total Equity Gross Minority Interest", "Common Stock Equity"),
    "total_debt": ("Total Debt", "Long Term Debt And Capital Lease Obligation"),
    "receivables": ("Accounts Receivable", "Receivables", "Gross Accounts Receivable"),
    "inventory": ("Inventory",),
    "total_assets": ("Total Assets",),
    "current_assets": ("Current Assets", "Total Current Assets"),
    "current_liabilities": ("Current Liabilities", "Total Current Liabilities"),
    "operating_cashflow": ("Operating Cash Flow", "Cash Flow From Continuing Operating Activities"),
    "capex": ("Capital Expenditure",),
    "free_cashflow": ("Free Cash Flow",),
}


def _row(df: pd.DataFrame | None, metric: str) -> pd.Series | None:
    """Pull a statement row by any of its known aliases, newest first."""
    if df is None or df.empty:
        return None
    index = {str(i).strip(): i for i in df.index}
    for alias in ROW_ALIASES.get(metric, (metric,)):
        if alias in index:
            series = pd.to_numeric(df.loc[index[alias]], errors="coerce").dropna()
            if not series.empty:
                return series.sort_index(ascending=False)  # newest first
    return None


def _latest(series: pd.Series | None) -> float | None:
    if series is None or series.empty:
        return None
    value = series.iloc[0]
    return None if pd.isna(value) else float(value)


def _cagr(series: pd.Series | None, years: int = 3) -> float | None:
    """Compound annual growth rate, in percent, newest-first series.

    Returns None when the base is zero or negative - a CAGR measured from a
    loss is arithmetically defined but economically meaningless, and passing
    it downstream would let a company that lost money three years ago look
    like a growth story.
    """
    if series is None or len(series) < years + 1:
        return None
    latest, base = float(series.iloc[0]), float(series.iloc[years])
    if base <= 0 or latest <= 0:
        return None
    return ((latest / base) ** (1.0 / years) - 1.0) * 100.0


def _yoy_pct(series: pd.Series | None) -> float | None:
    if series is None or len(series) < 2:
        return None
    latest, prior = float(series.iloc[0]), float(series.iloc[1])
    if prior == 0:
        return None
    return (latest - prior) / abs(prior) * 100.0


def _fetch_raw(symbol: str) -> dict[str, Any]:
    import yfinance as yf

    ticker = yf.Ticker(symbol)
    info = dict(ticker.info or {})

    statements: dict[str, pd.DataFrame] = {}
    for name, attribute in (("income", "income_stmt"), ("balance", "balance_sheet"), ("cashflow", "cashflow")):
        try:
            df = getattr(ticker, attribute)
            if df is not None and not df.empty:
                statements[name] = df
        except Exception as exc:
            log.debug("%s %s unavailable: %s", symbol, name, exc)

    return {"info": info, "statements": statements}


def get_financial_statements(stock: StockIdentity | str) -> DataResult[dict[str, pd.DataFrame]]:
    """Income statement, balance sheet and cash flow, five years annual."""
    identity = StockIdentity(stock) if isinstance(stock, str) else stock
    raw = cached_fetch("fundamentals", make_key("raw", identity.yahoo), lambda: _fetch_raw(identity.yahoo))

    if raw is None:
        return DataResult.unavailable(SOURCE, f"no fundamentals for {identity.symbol}")

    statements = raw.get("statements", {})
    if not statements:
        return DataResult.empty(SOURCE, f"no statements published for {identity.symbol}")

    status = DataStatus.OK if len(statements) == 3 else DataStatus.PARTIAL
    missing = {"income", "balance", "cashflow"} - set(statements)
    note = None if not missing else f"missing: {', '.join(sorted(missing))}"

    return DataResult(value=statements, status=status, source=SOURCE, as_of=date.today(), note=note)


def get_fundamentals(stock: StockIdentity | str) -> DataResult[dict[str, Any]]:
    """Everything the quality gate and the equity desk read.

    Returns a flat dict so it drops straight into the evidence pack. Values
    are None where genuinely unknown rather than zero, because a bot must be
    able to tell "no debt" from "we don't know the debt".
    """
    identity = StockIdentity(stock) if isinstance(stock, str) else stock
    raw = cached_fetch("fundamentals", make_key("raw", identity.yahoo), lambda: _fetch_raw(identity.yahoo))

    if raw is None:
        return DataResult.unavailable(SOURCE, f"no fundamentals for {identity.symbol}")

    info: dict[str, Any] = raw.get("info", {})
    statements: dict[str, pd.DataFrame] = raw.get("statements", {})
    income = statements.get("income")
    balance = statements.get("balance")
    cashflow = statements.get("cashflow")

    revenue = _row(income, "revenue")
    net_income = _row(income, "net_income")
    equity = _row(balance, "equity")
    total_debt = _row(balance, "total_debt")
    receivables = _row(balance, "receivables")
    op_cashflow = _row(cashflow, "operating_cashflow")

    market_cap = info.get("marketCap")

    # Yahoo's returnOnEquity is null for many Indian listings, so prefer the
    # computed figure and fall back to the reported one.
    roe = None
    latest_ni, latest_eq = _latest(net_income), _latest(equity)
    if latest_ni is not None and latest_eq and latest_eq > 0:
        roe = latest_ni / latest_eq * 100.0
    elif info.get("returnOnEquity") is not None:
        roe = float(info["returnOnEquity"]) * 100.0

    debt_to_equity = None
    latest_debt = _latest(total_debt)
    if latest_debt is not None and latest_eq and latest_eq > 0:
        debt_to_equity = latest_debt / latest_eq
    elif info.get("debtToEquity") is not None:
        debt_to_equity = float(info["debtToEquity"]) / 100.0  # Yahoo reports a percentage

    profitable_years = None
    if net_income is not None and len(net_income) >= 1:
        recent = net_income.iloc[: min(4, len(net_income))]
        profitable_years = int((recent > 0).sum())

    metrics: dict[str, Any] = {
        "symbol": identity.symbol,
        "name": info.get("longName") or info.get("shortName"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        # Size and valuation
        "market_cap": market_cap,
        "market_cap_cr": round(market_cap / CRORE, 1) if market_cap else None,
        "pe_trailing": info.get("trailingPE"),
        "pe_forward": info.get("forwardPE"),
        "price_to_book": info.get("priceToBook"),
        "book_value": info.get("bookValue"),
        "eps_trailing": info.get("trailingEps"),
        "dividend_yield_pct": info.get("dividendYield"),
        # Returns and margins
        "roe_pct": roe,
        "profit_margin_pct": (info.get("profitMargins") or 0) * 100.0 if info.get("profitMargins") is not None else None,
        "operating_margin_pct": (info.get("operatingMargins") or 0) * 100.0 if info.get("operatingMargins") is not None else None,
        # Growth
        "revenue_cagr_3y_pct": _cagr(revenue, 3),
        "profit_cagr_3y_pct": _cagr(net_income, 3),
        "revenue_growth_yoy_pct": _yoy_pct(revenue),
        "profit_growth_yoy_pct": _yoy_pct(net_income),
        # Balance sheet
        "debt_to_equity": debt_to_equity,
        "total_debt_cr": round(latest_debt / CRORE, 1) if latest_debt else None,
        "equity_cr": round(latest_eq / CRORE, 1) if latest_eq else None,
        "current_ratio": info.get("currentRatio"),
        "profitable_years_of_4": profitable_years,
        # Ownership (Yahoo's view; NSE's shareholding pattern is authoritative)
        "held_pct_insiders": (info.get("heldPercentInsiders") or 0) * 100.0 if info.get("heldPercentInsiders") is not None else None,
        "held_pct_institutions": (info.get("heldPercentInstitutions") or 0) * 100.0 if info.get("heldPercentInstitutions") is not None else None,
        "beta": info.get("beta"),
        # Provenance, so bots can see how much history backed the ratios
        "statement_years": int(len(revenue)) if revenue is not None else 0,
    }

    metrics["forensics"] = compute_forensics(income, balance, cashflow)

    filled = sum(1 for k, v in metrics.items() if k != "forensics" and v is not None)
    status = DataStatus.OK if filled >= 12 else DataStatus.PARTIAL
    note = None if status is DataStatus.OK else f"only {filled} fields resolved"

    return DataResult(value=metrics, status=status, source=SOURCE, as_of=date.today(), note=note)


def compute_forensics(
    income: pd.DataFrame | None,
    balance: pd.DataFrame | None,
    cashflow: pd.DataFrame | None,
) -> dict[str, Any]:
    """Accounting red-flag arithmetic for the Financial Forensics Analyst.

    Each entry is a plain measurement with the reasoning attached, not a
    verdict. The bot decides what they mean together; a single elevated
    ratio is ordinary, three pointing the same way is a pattern.
    """
    revenue = _row(income, "revenue")
    net_income = _row(income, "net_income")
    receivables = _row(balance, "receivables")
    inventory = _row(balance, "inventory")
    total_debt = _row(balance, "total_debt")
    op_cashflow = _row(cashflow, "operating_cashflow")

    flags: dict[str, Any] = {}

    # Cash conversion: profit that never becomes cash is the classic signal.
    if op_cashflow is not None and net_income is not None:
        pairs = [
            (float(c), float(n))
            for c, n in zip(op_cashflow, net_income)
            if pd.notna(c) and pd.notna(n) and n > 0
        ]
        if pairs:
            ratios = [c / n for c, n in pairs]
            flags["cfo_to_pat_latest"] = round(ratios[0], 2)
            flags["cfo_to_pat_avg_3y"] = round(float(np.mean(ratios[:3])), 2)
            flags["years_cfo_below_pat"] = int(sum(1 for r in ratios[:4] if r < 1.0))
            flags["cfo_to_pat_note"] = (
                "Operating cash flow below reported profit means earnings are not "
                "converting to cash. One year can be working-capital timing; a run "
                "of years is the pattern worth investigating."
            )

    # Receivables outrunning sales: revenue booked but not collected.
    if receivables is not None and revenue is not None and len(receivables) >= 2 and len(revenue) >= 2:
        receivable_growth = _yoy_pct(receivables)
        revenue_growth = _yoy_pct(revenue)
        if receivable_growth is not None and revenue_growth is not None:
            flags["receivables_growth_yoy_pct"] = round(receivable_growth, 1)
            flags["revenue_growth_yoy_pct"] = round(revenue_growth, 1)
            flags["receivables_vs_sales_gap_pct"] = round(receivable_growth - revenue_growth, 1)
            flags["receivables_note"] = (
                "Receivables growing materially faster than sales can mean revenue is "
                "being recognised on terms that are not being collected."
            )
        latest_rev, latest_rec = _latest(revenue), _latest(receivables)
        if latest_rev and latest_rec and latest_rev > 0:
            flags["receivable_days"] = round(latest_rec / latest_rev * 365.0, 0)

    # Inventory building faster than sales can precede a write-down.
    if inventory is not None and revenue is not None:
        inventory_growth, revenue_growth = _yoy_pct(inventory), _yoy_pct(revenue)
        if inventory_growth is not None and revenue_growth is not None:
            flags["inventory_vs_sales_gap_pct"] = round(inventory_growth - revenue_growth, 1)

    # Leverage trend matters more than the level for a dip candidate.
    if total_debt is not None and len(total_debt) >= 2:
        debt_growth = _yoy_pct(total_debt)
        if debt_growth is not None:
            flags["debt_growth_yoy_pct"] = round(debt_growth, 1)

    if revenue is not None and net_income is not None and len(revenue) >= 3:
        margins = [
            float(n) / float(r) * 100.0
            for r, n in zip(revenue, net_income)
            if pd.notna(r) and pd.notna(n) and r > 0
        ]
        if len(margins) >= 3:
            flags["net_margin_trend_pct"] = [round(m, 1) for m in margins[:4]]
            # A quarter-point swing is noise, not a trend. Without a flat band
            # every company reads as expanding or compressing on rounding.
            change = margins[0] - margins[1]
            if abs(change) < 0.25:
                flags["margin_direction"] = "flat"
            else:
                flags["margin_direction"] = "expanding" if change > 0 else "compressing"

    flags["data_years"] = int(len(revenue)) if revenue is not None else 0
    return flags


def passes_quality_gate(metrics: dict[str, Any], cfg: Any) -> tuple[bool, list[str]]:
    """Stage 1. Returns the verdict and the reason for every failure.

    An individual unknown value does not fail the gate. A missing ROE is an
    absence of evidence, and rejecting on it would quietly drop companies
    whose data Yahoo happens not to carry rather than companies that are
    actually poor.

    Wholesale absence is different, and is a failure. A delisted or renamed
    symbol comes back with every field null, and without this check it would
    sail through the gate having been tested against nothing at all - the
    worst possible outcome, since it looks like a pass.
    """
    reasons: list[str] = []

    checkable = [
        metrics.get("market_cap_cr"),
        metrics.get("roe_pct"),
        metrics.get("debt_to_equity"),
        metrics.get("revenue_cagr_3y_pct"),
        metrics.get("profitable_years_of_4"),
    ]
    known = sum(1 for value in checkable if value is not None)
    min_known = int(cfg.get("quality_gate.min_known_metrics", 3))
    if known < min_known:
        return False, [
            f"insufficient fundamental data: only {known} of {len(checkable)} gate "
            f"metrics available (need {min_known}); symbol may be delisted or renamed"
        ]

    market_cap_cr = metrics.get("market_cap_cr")
    min_cap = cfg.get("quality_gate.min_market_cap_cr", 5000)
    if market_cap_cr is not None and market_cap_cr < min_cap:
        reasons.append(f"market cap Rs {market_cap_cr:,.0f} cr below Rs {min_cap:,} cr")

    roe = metrics.get("roe_pct")
    min_roe = cfg.get("quality_gate.min_roe_pct", 12.0)
    if roe is not None and roe < min_roe:
        reasons.append(f"ROE {roe:.1f}% below {min_roe}%")

    sector = (metrics.get("sector") or "").strip()
    exempt = set(cfg.get("quality_gate.debt_exempt_sectors", []) or [])
    if sector not in exempt:
        debt_equity = metrics.get("debt_to_equity")
        max_de = cfg.get("quality_gate.max_debt_to_equity", 1.5)
        if debt_equity is not None and debt_equity > max_de:
            reasons.append(f"debt/equity {debt_equity:.2f} above {max_de}")

    cagr = metrics.get("revenue_cagr_3y_pct")
    min_cagr = cfg.get("quality_gate.min_revenue_cagr_3y_pct", 0.0)
    if cagr is not None and cagr < min_cagr:
        reasons.append(f"3y revenue CAGR {cagr:.1f}% below {min_cagr}%")

    profitable = metrics.get("profitable_years_of_4")
    min_profitable = cfg.get("quality_gate.min_profitable_years_of_4", 3)
    if profitable is not None and profitable < min_profitable:
        reasons.append(f"profitable in only {profitable} of last 4 years")

    return (not reasons), reasons
