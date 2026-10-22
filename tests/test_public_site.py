"""The public demo page: shows the research, never the owner's portfolio."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from src import db, public_site
from src.config import load_config

CFG = load_config()
NOW = datetime(2026, 10, 22, 8, 5)


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path/'site.db'}")
    monkeypatch.setattr(db, "_engine", engine)
    db.init_db()
    return engine


def seed():
    with db.connection() as conn:
        scan_id = conn.execute(db.scans.insert().values(
            run_at=NOW, trade_date=date(2026, 10, 21), universe="NIFTY 500", universe_size=501,
            passed_dip=27, passed_quality=15, passed_delivery=15, status="complete")).inserted_primary_key[0]
        conn.execute(db.candidates.insert().values(scan_id=scan_id, symbol="SAILIFE", rank=1, close=812.4,
                                                   drawdown_pct=18.2, screen_score=61.0))
        conn.execute(db.candidates.insert().values(scan_id=scan_id, symbol="J&KBANK", rank=2, close=138.2,
                                                   drawdown_pct=28.0, screen_score=55.0))
        conn.execute(db.stocks.insert().values(symbol="SAILIFE", name="Sai Life Sciences", sector="Healthcare"))
        run_id = conn.execute(db.committee_runs.insert().values(
            symbol="SAILIFE", run_at=NOW, trade_date=date(2026, 10, 21), price_at_run=812.4, stance="BUY",
            conviction=56.6, sizing_json=json.dumps({"total_value": 40000, "total_shares": 49}))).inserted_primary_key[0]
        conn.execute(db.bot_verdicts.insert().values(
            run_id=run_id, bot_id="valuation_analyst", desk="equity", role="analyst", score=1.7,
            evidence_json=json.dumps([{"field": "valuation_label", "value": "CHEAP"}]),
            key_findings_json=json.dumps(["Valuation: CHEAP"])))
        conn.execute(db.committee_runs.insert().values(
            symbol="J&KBANK", run_at=NOW, trade_date=date(2026, 10, 21), price_at_run=138.2, stance="WATCH",
            conviction=48.1))
        # The owner's private book: must never reach the page.
        conn.execute(db.positions.insert().values(symbol="SECRETHOLD", status="open", quantity=313,
                                                  avg_entry_price=987.65, invested_value=309134.45))
        conn.execute(db.transactions.insert().values(symbol="SECRETHOLD", side="BUY", trade_date=date(2026, 10, 1),
                                                     quantity=313, price=987.65))
        conn.execute(db.intraday_trades.insert().values(
            day=date(2026, 10, 21), symbol="BANKBEES", mode="paper", side="SHORT", entry_at=datetime(2026, 10, 21, 10, 40),
            entry=575.28, stop=577.0, units=1303, exit_reason="target_2.5R", gross=5017.85, charges=334.66,
            net=4683.18, r=2.1, closed=True))


def page(**kw):
    return public_site.render(public_site.gather(CFG, now=NOW, **kw))


def test_the_page_shows_the_scan_verdicts_calls_and_paper_test(fresh_db):
    seed()
    html = page()
    assert "Wed 21 Oct 2026" in html
    for part in ("501", "27", "SAILIFE", "Healthcare", "Buy", "57", "Cheap", "Watch", "BANKBEES", "1 / 40",
                 "2.10R", "target 2.5R"):
        assert part in html, part
    assert "J&amp;KBANK" in html and "J&KBANK" not in html      # escaped


def test_verdicts_lead_vetoes_say_so_and_the_unreviewed_fold_away(fresh_db):
    seed()
    with db.connection() as conn:
        scan_id = conn.execute(db.scans.select()).first().id
        for rank, symbol in ((3, "VETOCO"), (4, "WAITCO")):
            conn.execute(db.candidates.insert().values(scan_id=scan_id, symbol=symbol, rank=rank, close=100.0,
                                                       drawdown_pct=15.0, screen_score=50.0))
        conn.execute(db.committee_runs.insert().values(symbol="VETOCO", run_at=NOW, trade_date=date(2026, 10, 21),
                                                       price_at_run=100.0, stance="NO_BUY", conviction=0.0,
                                                       forensics_veto=True))
    html = page()
    table = html[html.index("<tbody>"):html.index("</tbody>")]
    assert table.index("SAILIFE") < table.index("J&amp;KBANK") < table.index("VETOCO")   # buy, watch, no
    assert "Vetoed" in table and "WAITCO" not in table
    assert "1 more passed the screen" in html and "WAITCO" in html
    assert '<span class="funnel-n">3</span><span class="funnel-label">Reviewed' in html


def test_the_owners_holdings_and_sizes_never_appear(fresh_db):
    seed()
    html = page()
    for private in ("SECRETHOLD", "313", "987.65", "309,134", "309134", "40,000", "40000"):
        assert private not in html, private


def test_the_page_never_reads_a_portfolio_table(fresh_db):
    from sqlalchemy import event

    seed()
    seen = []
    event.listen(fresh_db, "before_cursor_execute",
                 lambda conn, cursor, statement, *rest: seen.append(statement.lower()))
    page()
    sql = " ".join(seen)
    assert "from scans" in sql, "the listener saw no queries"
    for table in ("positions", "transactions", "trades", "telegram_inbox", "alerts_sent"):
        assert re.search(rf"\b(from|join)\s+{table}\b", sql) is None, table
    # The same pattern must find a table the page does read, or the check above proves nothing.
    assert re.search(r"\b(from|join)\s+scans\b", sql)


def test_an_empty_database_renders_its_empty_states(fresh_db):
    html = page()
    assert "No scan has completed yet." in html
    assert "No buy calls in the last 60 days" in html
    assert "No paper trades yet." in html
    assert "0 / 40" in html


def test_the_market_line_uses_the_regime_when_available(fresh_db):
    seed()
    market = SimpleNamespace(label="DOWNTREND", pct_vs_dma=-4.6, as_of=date(2026, 10, 21), known=True)
    html = page(regime_fn=lambda: market)
    assert "4.6% below its 200-day average" in html and "downtrend" in html
    broken = page(regime_fn=lambda: (_ for _ in ()).throw(RuntimeError("Yahoo down")))
    assert "Market trend" not in broken and "SAILIFE" in broken    # still builds


def test_calls_older_than_60_days_drop_off(fresh_db):
    seed()
    with db.connection() as conn:
        conn.execute(db.committee_runs.insert().values(symbol="OLDCALL", run_at=NOW, trade_date=date(2026, 7, 1),
                                                       price_at_run=100.0, stance="BUY", conviction=58.0))
    assert "OLDCALL" not in page()


def test_nothing_on_the_page_starts_hidden(fresh_db):
    """Animations may never run (background tabs, link previews): they must not hide content."""
    seed()
    html = page()
    assert "opacity:0" not in html.replace(" ", "") and "scaleX(0)" not in html
