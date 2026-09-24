"""NSE data: delivery, ownership, disclosures and surveillance.

NSE sits behind Akamai bot detection. Plain `requests` gets a flat 403 no
matter what headers you send, because the block is on the TLS fingerprint,
not the User-Agent. `curl_cffi` impersonating Chrome clears it, and the
session must first load the homepage to collect cookies before any API call
will answer.

Endpoint reality as probed, because it is uneven and worth writing down:

  working   ind_nifty500list.csv        index constituents, with sector
  working   EQUITY_L.csv                all listed equities
  working   sec_bhavdata_full_*.csv     daily OHLC + DELIV_QTY + DELIV_PER
  working   api/corporates-pit          SEBI PIT insider disclosures
  working   api/corporate-share-holdings-master   quarterly shareholding
  working   api/corporate-announcements filings
  working   content/equities/bulk.csv   bulk deals, current day only
  working   content/equities/block.csv  block deals, current day only
  working   content/fo/fo_secban.csv    F&O ban list
  blocked   api/quote-equity            403
  blocked   api/historical/short-selling  503

The daily-deal and ban files carry only the current session, so history is
built by the daily scan accumulating them rather than fetched in one shot.
Anything unavailable returns a DataResult that says so - the committee is
built to report which desk was blind, not to crash.
"""

from __future__ import annotations

import io
import logging
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

from src.data.cache import NotFound, cached_fetch, make_key
from src.data.provider import DataResult, DataStatus, StockIdentity

log = logging.getLogger(__name__)

SOURCE = "nse"

BASE = "https://www.nseindia.com"
ARCHIVES = "https://nsearchives.nseindia.com"

INDEX_FILES = {
    "NIFTY 50": "ind_nifty50list.csv",
    "NIFTY 100": "ind_nifty100list.csv",
    "NIFTY 200": "ind_nifty200list.csv",
    "NIFTY 500": "ind_nifty500list.csv",
    "NIFTY MIDCAP 150": "ind_niftymidcap150list.csv",
    "NIFTY SMALLCAP 250": "ind_niftysmallcap250list.csv",
}


class NSESession:
    """A cookie-warmed, browser-impersonating session with lazy re-warming.

    NSE's cookies expire after a few minutes of idling and the API then
    answers 403. Rather than warming on a timer, the session re-warms when a
    call actually fails and retries once - which is both cheaper and more
    reliable than guessing the expiry.
    """

    WARM_AFTER_SECONDS = 240

    def __init__(self, impersonate: str = "chrome"):
        self._impersonate = impersonate
        self._session: Any = None
        self._warmed_at: float = 0.0
        self._lock = threading.Lock()

    def _build(self) -> Any:
        from curl_cffi import requests as creq

        session = creq.Session(impersonate=self._impersonate)
        session.headers.update(
            {
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
        )
        return session

    def _warm(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and self._session is not None and (now - self._warmed_at) < self.WARM_AFTER_SECONDS:
            return

        self._session = self._build()
        try:
            self._session.get(BASE, timeout=25)
            time.sleep(0.8)  # NSE sets cookies a beat after the response
            self._warmed_at = time.monotonic()
            log.debug("NSE session warmed (%d cookies)", len(self._session.cookies))
        except Exception as exc:
            log.warning("NSE cookie warm-up failed: %s", exc)

    def get(self, url: str, *, referer: str | None = None, json_accept: bool = True, timeout: int = 30) -> Any:
        """GET with one automatic re-warm-and-retry on a bot-wall response."""
        with self._lock:
            self._warm()
            session = self._session

        headers = {"Accept": "application/json, text/plain, */*" if json_accept else "*/*"}
        headers["Referer"] = referer or BASE

        response = session.get(url, headers=headers, timeout=timeout)

        if response.status_code in (401, 403, 503):
            log.debug("NSE returned %s for %s - re-warming", response.status_code, url)
            with self._lock:
                self._warm(force=True)
                session = self._session
            if referer:
                try:
                    session.get(referer, headers={"Accept": "text/html,*/*;q=0.8"}, timeout=timeout)
                    time.sleep(0.8)
                except Exception:
                    pass
            response = session.get(url, headers=headers, timeout=timeout)

        return response


_session: NSESession | None = None
_session_lock = threading.Lock()


def get_session() -> NSESession:
    global _session
    with _session_lock:
        if _session is None:
            _session = NSESession()
        return _session


def _fetch_text(url: str, *, referer: str | None = None, json_accept: bool = True) -> str:
    response = get_session().get(url, referer=referer, json_accept=json_accept)
    if response.status_code == 404:
        # The file genuinely is not there - a market holiday, or a bhavcopy
        # not yet published. Retrying cannot conjure it.
        raise NotFound(f"NSE has no file at {url}")
    if response.status_code != 200:
        raise RuntimeError(f"NSE {response.status_code} for {url}")
    return response.text


def _fetch_json(url: str, *, referer: str | None = None) -> Any:
    response = get_session().get(url, referer=referer)
    if response.status_code != 200:
        raise RuntimeError(f"NSE {response.status_code} for {url}")
    return response.json()


def _read_csv(text: str, *, skiprows: int = 0) -> pd.DataFrame:
    df = pd.read_csv(io.StringIO(text), skiprows=skiprows)
    df.columns = [str(c).strip() for c in df.columns]
    for column in df.columns:
        if df[column].dtype == object:
            df[column] = df[column].astype(str).str.strip()
    return df


def _to_number(series: pd.Series) -> pd.Series:
    """NSE embeds commas and uses '-' for nil. Coerce both to numbers."""
    return pd.to_numeric(
        series.astype(str).str.replace(",", "", regex=False).replace({"-": None, "": None, "nan": None}),
        errors="coerce",
    )


# --- Universe ---------------------------------------------------------------


def get_universe(index: str = "NIFTY 500", *, force_refresh: bool = False) -> DataResult[list[StockIdentity]]:
    """Index constituents, including the sector label NSE ships with them."""
    filename = INDEX_FILES.get(index.upper())
    if filename is None:
        return DataResult.unavailable(SOURCE, f"unknown index {index!r}")

    url = f"{ARCHIVES}/content/indices/{filename}"

    def fetch() -> pd.DataFrame:
        return _read_csv(_fetch_text(url, json_accept=False))

    df = cached_fetch("universe", make_key("universe", index), fetch, force_refresh=force_refresh)
    if df is None or df.empty:
        return DataResult.unavailable(SOURCE, f"could not fetch constituents for {index}")

    stocks = [
        StockIdentity(
            symbol=str(row.get("Symbol", "")).strip().upper(),
            name=str(row.get("Company Name", "")).strip() or None,
            sector=str(row.get("Industry", "")).strip() or None,
            isin=str(row.get("ISIN Code", "")).strip() or None,
        )
        for _, row in df.iterrows()
        if str(row.get("Symbol", "")).strip()
    ]

    return DataResult(value=stocks, status=DataStatus.OK, source=SOURCE, as_of=date.today())


# --- Delivery (the Stage 3 edge) --------------------------------------------


def get_bhavcopy(trade_date: date, *, force_refresh: bool = False) -> DataResult[pd.DataFrame]:
    """Full security-wise bhavcopy for one session, with delivery columns.

    Returns NOT_APPLICABLE for weekends and holidays rather than
    UNAVAILABLE - a missing file for a non-trading day is the correct
    answer, not a failure, and conflating the two would have the committee
    reporting a data outage every Monday morning.
    """
    if trade_date.weekday() >= 5:
        return DataResult.empty(SOURCE, f"{trade_date.isoformat()} is a weekend")

    stamp = trade_date.strftime("%d%m%Y")
    url = f"{ARCHIVES}/products/content/sec_bhavdata_full_{stamp}.csv"

    def fetch() -> pd.DataFrame:
        text = _fetch_text(url, json_accept=False)
        df = _read_csv(text)
        df["SYMBOL"] = df["SYMBOL"].str.upper()
        for column in [
            "PREV_CLOSE", "OPEN_PRICE", "HIGH_PRICE", "LOW_PRICE", "LAST_PRICE",
            "CLOSE_PRICE", "AVG_PRICE", "TTL_TRD_QNTY", "TURNOVER_LACS",
            "NO_OF_TRADES", "DELIV_QTY", "DELIV_PER",
        ]:
            if column in df.columns:
                df[column] = _to_number(df[column])
        df["trade_date"] = pd.Timestamp(trade_date)
        return df

    # Settled sessions never change, so cache them effectively forever.
    ttl = 24 * 365.0 if trade_date < date.today() else 6.0
    df = cached_fetch(
        "delivery", make_key("bhavcopy", stamp), fetch, ttl_hours=ttl, force_refresh=force_refresh
    )

    if df is None:
        return DataResult.empty(SOURCE, f"no bhavcopy for {trade_date.isoformat()} (holiday or not yet published)")
    return DataResult(value=df, status=DataStatus.OK, source=SOURCE, as_of=trade_date)


def get_delivery_history(
    stock: StockIdentity | str,
    days: int = 120,
    *,
    end: date | None = None,
    use_database: bool = True,
) -> DataResult[pd.DataFrame]:
    """Per-session delivery history for one symbol.

    Reads from the `price_bars` table first and walks back through daily
    bhavcopies only for dates the database is missing.

    That ordering matters in deployment. The disk cache that makes the
    bhavcopy walk cheap locally does not exist on Streamlit Cloud or a GitHub
    Actions runner - both are ephemeral - so without a database-backed store
    every run would re-fetch the same sixty files and delivery history could
    never grow beyond what a single run could reach.
    """
    identity = StockIdentity(stock) if isinstance(stock, str) else stock
    end_date = end or last_completed_session()

    if use_database:
        stored = _delivery_from_db(identity.symbol, days, end_date)
        if stored is not None and len(stored) >= days * 0.9:
            return DataResult(
                value=stored, status=DataStatus.OK, source="database",
                as_of=stored.index[-1].date(),
            )

    rows: list[dict[str, Any]] = []
    misses = 0
    cursor = end_date
    # Calendar span needed to cover `days` sessions, plus slack for holidays.
    horizon = cursor - timedelta(days=int(days * 1.6) + 20)

    while cursor >= horizon and len(rows) < days:
        if cursor.weekday() < 5:
            result = get_bhavcopy(cursor)
            if result.value is not None:
                df = result.value
                match = df[(df["SYMBOL"] == identity.symbol) & (df.get("SERIES", "EQ").astype(str).str.strip() == "EQ")]
                if not match.empty:
                    row = match.iloc[0]
                    rows.append(
                        {
                            "date": pd.Timestamp(cursor),
                            "open": row.get("OPEN_PRICE"),
                            "high": row.get("HIGH_PRICE"),
                            "low": row.get("LOW_PRICE"),
                            "close": row.get("CLOSE_PRICE"),
                            "prev_close": row.get("PREV_CLOSE"),
                            "volume": row.get("TTL_TRD_QNTY"),
                            "turnover_lacs": row.get("TURNOVER_LACS"),
                            "trades": row.get("NO_OF_TRADES"),
                            "deliverable_qty": row.get("DELIV_QTY"),
                            "delivery_pct": row.get("DELIV_PER"),
                        }
                    )
            elif result.status is DataStatus.UNAVAILABLE:
                misses += 1
                if misses > 10:
                    log.warning("Too many bhavcopy failures, stopping delivery walk for %s", identity.symbol)
                    break
        cursor -= timedelta(days=1)

    if not rows:
        return DataResult.unavailable(SOURCE, f"no delivery rows for {identity.symbol}")

    df = pd.DataFrame(rows).set_index("date").sort_index()
    status = DataStatus.OK if len(df) >= days * 0.6 else DataStatus.PARTIAL
    note = None if status is DataStatus.OK else f"only {len(df)} of {days} sessions available"

    return DataResult(value=df, status=status, source=SOURCE, as_of=df.index[-1].date(), note=note)


# --- Ownership and disclosures ----------------------------------------------


#: Person categories NSE uses for promoters. Their trades carry far more
#: signal than an employee exercising ESOPs, so they are tagged separately.
PROMOTER_CATEGORIES = ("promoter", "promoter group", "promoters")


def get_insider_trades(stock: StockIdentity | str, days: int = 90) -> DataResult[pd.DataFrame]:
    """SEBI PIT disclosures: who acquired or disposed, how much, and how.

    NSE's `from_date`/`to_date` parameters on this endpoint silently return
    an empty set, so the window is applied here instead: fetch the feed
    unfiltered and cut it by disclosure date in pandas.
    """
    identity = StockIdentity(stock) if isinstance(stock, str) else stock

    url = f"{BASE}/api/corporates-pit?index=equities&symbol={identity.symbol}"
    referer = f"{BASE}/companies-listing/corporate-filings-insider-trading"

    def fetch() -> pd.DataFrame:
        payload = _fetch_json(url, referer=referer)
        records = payload.get("data", payload) if isinstance(payload, dict) else payload
        return pd.DataFrame(records or [])

    df = cached_fetch("insider", make_key("pit", identity.symbol), fetch)

    if df is None:
        return DataResult.unavailable(SOURCE, f"insider feed unavailable for {identity.symbol}")
    if df.empty:
        return DataResult.empty(SOURCE, "no insider disclosures on record")

    df = df.copy()
    for column in ("secAcq", "secVal", "befAcqSharesNo", "afterAcqSharesNo"):
        if column in df.columns:
            df[column] = _to_number(df[column])

    if "date" in df.columns:
        df["disclosed_at"] = pd.to_datetime(
            df["date"].astype(str).str.strip(), format="%d-%b-%Y %H:%M", errors="coerce"
        )
        cutoff = pd.Timestamp(date.today() - timedelta(days=days))
        recent = df[df["disclosed_at"] >= cutoff]
    else:
        recent = df

    if recent.empty:
        latest = df["disclosed_at"].max() if "disclosed_at" in df.columns else None
        note = f"no disclosures in the last {days} days"
        if pd.notna(latest):
            note += f"; most recent was {latest.date().isoformat()}"
        return DataResult.empty(SOURCE, note)

    recent = recent.copy()

    # Direction and standing are what the analyst actually reasons over, so
    # derive them here rather than making every bot re-parse NSE's wording.
    if "tdpTransactionType" in recent.columns:
        side = recent["tdpTransactionType"].astype(str).str.strip().str.lower()
        recent["is_buy"] = side.str.startswith("buy")
        recent["is_sell"] = side.str.startswith("sell")
        direction = recent["is_buy"].map({True: 1, False: -1}).where(
            recent["is_buy"] | recent["is_sell"]
        )
        recent["signed_qty"] = recent.get("secAcq") * direction
        recent["signed_value"] = recent.get("secVal") * direction

    if "personCategory" in recent.columns:
        recent["is_promoter"] = (
            recent["personCategory"].astype(str).str.strip().str.lower().isin(PROMOTER_CATEGORIES)
        )

    as_of = recent["disclosed_at"].max().date() if "disclosed_at" in recent.columns else date.today()
    return DataResult(value=recent, status=DataStatus.OK, source=SOURCE, as_of=as_of)


def get_shareholding_pattern(stock: StockIdentity | str) -> DataResult[pd.DataFrame]:
    """Quarterly promoter / FII / DII / public holding percentages."""
    identity = StockIdentity(stock) if isinstance(stock, str) else stock
    url = f"{BASE}/api/corporate-share-holdings-master?index=equities&symbol={identity.symbol}"
    referer = f"{BASE}/companies-listing/corporate-filings-shareholding-pattern"

    def fetch() -> pd.DataFrame:
        payload = _fetch_json(url, referer=referer)
        records = payload.get("data", payload) if isinstance(payload, dict) else payload
        return pd.DataFrame(records or [])

    df = cached_fetch("shareholding", make_key("shp", identity.symbol), fetch)

    if df is None:
        return DataResult.unavailable(SOURCE, f"shareholding feed unavailable for {identity.symbol}")
    if df.empty:
        return DataResult.empty(SOURCE, "no shareholding filings returned")

    as_of = None
    if "date" in df.columns:
        parsed = pd.to_datetime(df["date"], errors="coerce", dayfirst=True)
        if parsed.notna().any():
            as_of = parsed.max().date()

    return DataResult(value=df, status=DataStatus.OK, source=SOURCE, as_of=as_of or date.today())


def get_corporate_announcements(stock: StockIdentity | str, days: int = 90) -> DataResult[pd.DataFrame]:
    """Filings: results, board meetings, ratings, pledge disclosures."""
    identity = StockIdentity(stock) if isinstance(stock, str) else stock
    to_date = date.today()
    from_date = to_date - timedelta(days=days)

    url = (
        f"{BASE}/api/corporate-announcements?index=equities&symbol={identity.symbol}"
        f"&from_date={from_date.strftime('%d-%m-%Y')}&to_date={to_date.strftime('%d-%m-%Y')}"
    )
    referer = f"{BASE}/companies-listing/corporate-filings-announcements"

    def fetch() -> pd.DataFrame:
        payload = _fetch_json(url, referer=referer)
        records = payload.get("data", payload) if isinstance(payload, dict) else payload
        df = pd.DataFrame(records or [])
        # The attachment body can run to megabytes and is useless to the bots.
        return df.drop(columns=[c for c in ("attchmntText",) if c in df.columns])

    df = cached_fetch("news", make_key("ann", identity.symbol, days), fetch)

    if df is None:
        return DataResult.unavailable(SOURCE, f"announcements unavailable for {identity.symbol}")
    if df.empty:
        return DataResult.empty(SOURCE, f"no announcements in the last {days} days")

    return DataResult(value=df, status=DataStatus.OK, source=SOURCE, as_of=date.today())


# --- Market activity --------------------------------------------------------


def _deals_csv(kind: str) -> DataResult[pd.DataFrame]:
    """Current-day bulk or block deals.

    NSE only publishes the live file, so this is a daily snapshot. The scan
    job appends it to the database, and history accumulates from there.
    """
    url = f"{ARCHIVES}/content/equities/{kind}.csv"

    def fetch() -> pd.DataFrame:
        df = _read_csv(_fetch_text(url, json_accept=False))
        rename = {
            "Date": "date", "Symbol": "symbol", "Security Name": "name",
            "Client Name": "client", "Buy/Sell": "side",
            "Quantity Traded": "quantity",
            "Trade Price / Wght. Avg. Price": "price", "Remarks": "remarks",
        }
        df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
        if "symbol" in df.columns:
            df["symbol"] = df["symbol"].str.upper()
        for column in ("quantity", "price"):
            if column in df.columns:
                df[column] = _to_number(df[column])
        df["deal_type"] = kind
        return df

    df = cached_fetch("delivery", make_key("deals", kind, date.today()), fetch, ttl_hours=6.0)
    if df is None:
        return DataResult.unavailable(SOURCE, f"{kind} deals unavailable")
    return DataResult(value=df, status=DataStatus.OK, source=SOURCE, as_of=date.today())


def get_bulk_block_deals(stock: StockIdentity | str | None = None) -> DataResult[pd.DataFrame]:
    """Today's bulk and block deals, optionally filtered to one symbol.

    These matter mainly as an explanation: a single large block deal can
    spike a session's delivery percentage and look exactly like quiet
    accumulation. Knowing a deal happened is what tells the two apart.
    """
    frames: list[pd.DataFrame] = []
    notes: list[str] = []

    for kind in ("bulk", "block"):
        result = _deals_csv(kind)
        if result.value is not None and not result.value.empty:
            frames.append(result.value)
        else:
            notes.append(f"{kind} unavailable")

    if not frames:
        return DataResult.unavailable(SOURCE, "; ".join(notes) or "no deal data")

    df = pd.concat(frames, ignore_index=True)

    if stock is not None:
        identity = StockIdentity(stock) if isinstance(stock, str) else stock
        df = df[df["symbol"] == identity.symbol]
        if df.empty:
            return DataResult.empty(SOURCE, f"no bulk or block deals in {identity.symbol} today")

    status = DataStatus.OK if not notes else DataStatus.PARTIAL
    return DataResult(value=df, status=status, source=SOURCE, as_of=date.today(), note="; ".join(notes) or None)


def get_fo_ban_list() -> DataResult[list[str]]:
    """Securities banned from F&O trading - a sign of stressed positioning."""
    url = f"{ARCHIVES}/content/fo/fo_secban.csv"

    def fetch() -> list[str]:
        text = _fetch_text(url, json_accept=False)
        symbols: list[str] = []
        for line in text.splitlines()[1:]:  # first line is a title, not a header
            parts = line.split(",")
            if len(parts) >= 2 and parts[1].strip():
                symbols.append(parts[1].strip().upper())
        return symbols

    symbols = cached_fetch("delivery", make_key("foban", date.today()), fetch, ttl_hours=6.0)
    if symbols is None:
        return DataResult.unavailable(SOURCE, "F&O ban list unavailable")
    return DataResult(value=symbols, status=DataStatus.OK, source=SOURCE, as_of=date.today())


def get_surveillance_flags(stock: StockIdentity | str) -> DataResult[dict[str, Any]]:
    """Surveillance status for a symbol.

    NSE's ASM and GSM list files are not reachable from here, so this covers
    the F&O ban only and says as much. Reporting partial coverage honestly
    beats implying a clean bill of health we cannot actually verify.
    """
    identity = StockIdentity(stock) if isinstance(stock, str) else stock
    ban = get_fo_ban_list()

    if not ban.usable:
        return DataResult.unavailable(SOURCE, "no surveillance source reachable")

    return DataResult(
        value={
            "in_fo_ban": identity.symbol in (ban.value or []),
            "asm_checked": False,
            "gsm_checked": False,
        },
        status=DataStatus.PARTIAL,
        source=SOURCE,
        as_of=date.today(),
        note="ASM/GSM lists unreachable; F&O ban only",
    )


def get_short_selling(stock: StockIdentity | str, days: int = 30) -> DataResult[pd.DataFrame]:
    """NSE's short-selling report. Currently blocked (503) from this network."""
    return DataResult.unavailable(SOURCE, "NSE short-selling endpoint returns 503")


def get_equity_master() -> DataResult[pd.DataFrame]:
    """Every NSE-listed equity, with listing date and ISIN."""
    url = f"{ARCHIVES}/content/equities/EQUITY_L.csv"

    def fetch() -> pd.DataFrame:
        df = _read_csv(_fetch_text(url, json_accept=False))
        df["SYMBOL"] = df["SYMBOL"].str.upper()
        return df

    df = cached_fetch("universe", make_key("equity_master"), fetch)
    if df is None or df.empty:
        return DataResult.unavailable(SOURCE, "equity master unavailable")
    return DataResult(value=df, status=DataStatus.OK, source=SOURCE, as_of=date.today())


def _delivery_from_db(symbol: str, days: int, end: date) -> pd.DataFrame | None:
    """Read stored delivery bars. Returns None if the table is unreachable."""
    try:
        from sqlalchemy import and_, select

        from src import db

        db.init_db()
        with db.connection() as conn:
            rows = conn.execute(
                select(
                    db.price_bars.c.date, db.price_bars.c.open, db.price_bars.c.high,
                    db.price_bars.c.low, db.price_bars.c.close, db.price_bars.c.volume,
                    db.price_bars.c.deliverable_qty, db.price_bars.c.delivery_pct,
                    db.price_bars.c.turnover_lacs,
                )
                .where(
                    and_(
                        db.price_bars.c.symbol == symbol.upper(),
                        db.price_bars.c.date <= end,
                        db.price_bars.c.delivery_pct.isnot(None),
                    )
                )
                .order_by(db.price_bars.c.date.desc())
                .limit(days)
            ).fetchall()
    except Exception as exc:
        log.debug("Could not read stored delivery for %s: %s", symbol, exc)
        return None

    if not rows:
        return None

    frame = pd.DataFrame([dict(r._mapping) for r in rows])
    frame["date"] = pd.to_datetime(frame["date"])
    return frame.set_index("date").sort_index()


#: Rows per INSERT statement. pg8000 has no fast-executemany - passing a list
#: of dicts sends one round trip per row, which against a Neon instance on
#: another continent (~250ms) turns 3,500 rows into fifteen minutes of
#: apparent hang. A multi-VALUES statement makes it one round trip per chunk.
INSERT_CHUNK = 500


def store_delivery_bars(
    bhavcopy: pd.DataFrame,
    trade_date: date,
    *,
    symbols: set[str] | None = None,
) -> int:
    """Persist one session's delivery rows so history accumulates.

    Called by the scheduled job. Rows already present are skipped rather than
    updated - a settled session does not change, and re-running the job must
    be free.

    `symbols` restricts the write to the stocks actually being screened. The
    bhavcopy carries every NSE equity, around 3,500 of them, and storing the
    ~2,900 that are not in the universe costs round trips for data nothing
    reads.
    """
    from sqlalchemy import select

    from src import db

    if bhavcopy is None or bhavcopy.empty:
        return 0

    db.init_db()
    equities = bhavcopy[bhavcopy.get("SERIES", "EQ").astype(str).str.strip() == "EQ"]

    with db.connection() as conn:
        existing = {
            row.symbol
            for row in conn.execute(
                select(db.price_bars.c.symbol).where(db.price_bars.c.date == trade_date)
            )
        }

    rows = []
    for _, record in equities.iterrows():
        symbol = str(record.get("SYMBOL", "")).strip().upper()
        if not symbol or symbol in existing:
            continue
        if symbols is not None and symbol not in symbols:
            continue
        close = record.get("CLOSE_PRICE")
        if pd.isna(close):
            continue
        rows.append({
            "symbol": symbol,
            "date": trade_date,
            "open": _none_if_nan(record.get("OPEN_PRICE")),
            "high": _none_if_nan(record.get("HIGH_PRICE")),
            "low": _none_if_nan(record.get("LOW_PRICE")),
            "close": _none_if_nan(close),
            "volume": _none_if_nan(record.get("TTL_TRD_QNTY")),
            "deliverable_qty": _none_if_nan(record.get("DELIV_QTY")),
            "delivery_pct": _none_if_nan(record.get("DELIV_PER")),
            "turnover_lacs": _none_if_nan(record.get("TURNOVER_LACS")),
        })

    if not rows:
        log.info("No new delivery bars to store for %s", trade_date)
        return 0

    # One statement per chunk rather than one per row.
    for start in range(0, len(rows), INSERT_CHUNK):
        chunk = rows[start : start + INSERT_CHUNK]
        with db.connection() as conn:
            conn.execute(db.price_bars.insert().values(chunk))
        log.info(
            "  stored %d/%d delivery bars for %s",
            min(start + INSERT_CHUNK, len(rows)), len(rows), trade_date,
        )

    return len(rows)


def _none_if_nan(value: Any) -> float | None:
    return None if value is None or pd.isna(value) else float(value)


def backfill_delivery_bars(days: int = 90, *, end: date | None = None) -> dict[str, int]:
    """Walk recent bhavcopies into the database.

    Run once after deployment so the first scan has history to read rather
    than sixty NSE requests to make.
    """
    cursor = end or date.today()
    horizon = cursor - timedelta(days=int(days * 1.5))
    stored = sessions = 0

    while cursor >= horizon:
        if cursor.weekday() < 5:
            result = get_bhavcopy(cursor)
            if result.value is not None:
                stored += store_delivery_bars(result.value, cursor)
                sessions += 1
        cursor -= timedelta(days=1)

    return {"sessions": sessions, "bars_stored": stored}


#: NSE publishes the day's full bhavcopy around 18:00-18:30 IST. Before this
#: hour, today's session is treated as not yet available.
BHAVCOPY_READY_HOUR_IST = 19


def last_completed_session(now: datetime | None = None) -> date:
    """The most recent weekday whose bhavcopy should already be published.

    The scan runs at 07:17 IST, before the market opens, so "today" has no
    data yet. Asking for it wastes a request per stock (a 404 is not cached)
    and, worse, made the evening delivery store find nothing. Before 19:00 IST
    this is the previous weekday; from 19:00, today. Holidays are not known
    here - callers walk back past a missing file, as with last_trading_day.

    `now` is IST, naive. It defaults to db.now(), not datetime.now(), because
    the job runs on a UTC machine.
    """
    if now is None:
        from src import db

        now = db.now()
    day = now.date()
    if now.hour < BHAVCOPY_READY_HOUR_IST:
        day -= timedelta(days=1)
    return last_trading_day(day)


def last_trading_day(reference: date | None = None) -> date:
    """Most recent weekday at or before `reference`.

    Approximate by design: it skips weekends but not NSE holidays. Callers
    fetch and let a missing bhavcopy tell them it was a holiday, which is
    more reliable than maintaining a holiday calendar by hand.
    """
    cursor = reference or date.today()
    while cursor.weekday() >= 5:
        cursor -= timedelta(days=1)
    return cursor
