"""Every alert says which strategy it belongs to, and BUYs carry their exits."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from src.alerts import holdings_news, telegram
from src.config import load_config
from src.strategy import exit as exit_rules

CFG = load_config()


def _report():
    doctrine = exit_rules.build_doctrine("INDIANB", 842.0, CFG, stop_price=631.5).to_dict()
    return SimpleNamespace(
        symbol="INDIANB", conviction=64.0, sector="Banks", price=842.0,
        sizing={"is_buy": True, "recommendation": "BALANCED", "total_value": 50520.0,
                "total_shares": 60, "pct_of_capital": 5.1, "stop_price": 631.5,
                "risk_amount": 12630.0, "risk_pct_of_capital": 1.26, "tranches": []},
        exit_doctrine=doctrine, market_regime={}, desks=[], red_flags=[], coverage=1.0,
    )


def test_long_term_buy_is_labelled_and_states_stop_and_targets():
    body = telegram.buy_candidate(_report(), CFG)
    assert body.splitlines()[0] == "<b>LONG-TERM BUY: INDIANB</b>"
    assert "Stop-loss: Rs 631.50" in body
    assert "risking Rs 12,630" in body
    assert "sell 25% at Rs 1,052.50 (+25%)" in body
    assert "sell 25% at Rs 1,263.00 (+50%)" in body
    assert "falls 20% from its peak" in body


def test_exit_and_tax_alerts_are_labelled():
    signal = SimpleNamespace(action="EXIT", rule="hard_stop", message="Sell.", trim_pct=None)
    position = SimpleNamespace(gain_pct=lambda price: -26.0)
    assert telegram.exit_signal("INDIANB", signal, position, 620.0, CFG).startswith(
        "<b>LONG-TERM EXIT: INDIANB</b>")

    detail = {"days_to_ltcg": 20, "unrealised_profit": 10000, "stcg_due": 2000, "ltcg_due": 1250,
              "saving": 750, "lot_quantity": 30, "lot_date": "2025-10-10"}
    assert telegram.ltcg_warning("INDIANB", detail, CFG).startswith(
        "<b>LONG-TERM TAX DEADLINE: INDIANB</b>")


def test_news_alerts_carry_the_strategy():
    flag = holdings_news.NewsFlag("SBIN", "governance", "Auditor resigns", "NSE filing",
                                  None, held=10, strategy="swing")
    assert holdings_news.message(flag).startswith("<b>SWING NEWS: SBIN</b>")
    flag.strategy = "long_term"
    assert holdings_news.message(flag).startswith("<b>LONG-TERM NEWS: SBIN</b>")


def test_every_alert_header_parses_back_for_replies():
    """A reply to any alert must find the stock and the strategy again."""
    from src.alerts.telegram_inbox import REPLY_HEADER

    headers = {
        "LONG-TERM BUY: INDIANB": ("LONG-TERM", "BUY"),
        "LONG-TERM EXIT: INDIANB": ("LONG-TERM", "EXIT"),
        "LONG-TERM TRIM: INDIANB": ("LONG-TERM", "TRIM"),
        "LONG-TERM TAX DEADLINE: INDIANB": ("LONG-TERM", "TAX DEADLINE"),
        "SWING BUY: INDIANB": ("SWING", "BUY"),
        "BUY candidate: INDIANB": (None, "BUY candidate"),     # alerts sent before the labels
        "EXIT: INDIANB": (None, "EXIT"),
    }
    for header, (prefix, label) in headers.items():
        match = REPLY_HEADER.match(header + "\nmore text")
        assert match, header
        assert (match.group(1), match.group(2), match.group(3)) == (prefix, label, "INDIANB")


def test_summary_separates_the_strategies():
    scan = SimpleNamespace(universe_size=501, duration_seconds=500, passed_dip=15,
                           passed_delivery=1, trade_date=date(2026, 9, 24))
    body = telegram.scan_summary(
        scan, [{"symbol": "INDIANB", "close": 842.0, "drawdown_pct": 15.1,
                "conviction": 53.0, "stance": "WATCH"}],
        CFG, holdings=2, swing_holdings=1,
    )
    assert "LONG-TERM: 1 candidate" in body
    assert "your 2 long-term holdings" in body
    assert "1 swing position tracked" in body
