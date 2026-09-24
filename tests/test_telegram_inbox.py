"""Tests for recording trades from Telegram messages.

This path writes to the portfolio from the outside world, so the tests lean
on the failure cases: a stranger's message, the same message read twice, a
sale larger than the holding, an undo that must not touch anything else, and
a broker import that has to replace the estimate rather than double it.
Every network call - Telegram in both directions, and Yahoo - is stubbed.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest
from sqlalchemy import select

from src import db
from src import portfolio as pf
from src.alerts import telegram_inbox as inbox
from src.alerts.telegram_inbox import Command, Problem, parse

OWNER = "111222333"
STRANGER = "999888777"

BUY_ALERT = "BUY candidate: INDIANB\nConviction 64/100 · Banks\nPrice Rs 842.00"
EXIT_ALERT = "EXIT: INDIANB\nstop loss · -8.1% at Rs 774.00"
REVIEW_ALERT = "REVIEW: INDIANB\nthesis check"
REGIME_ALERT = "Market regime: MIXED -> DOWNTREND\nNifty 500 is 0.3% below"


# --- Parsing ----------------------------------------------------------------------


@pytest.mark.parametrize("text, reply, expected", [
    ("INDIANB 30", None, ("BUY", "INDIANB", 30, None)),
    ("bought INDIANB 30", None, ("BUY", "INDIANB", 30, None)),
    ("I bought 30 shares of indianb", None, ("BUY", "INDIANB", 30, None)),
    ("sold INDIANB 30", None, ("SELL", "INDIANB", 30, None)),
    ("sell 10 indianb", None, ("SELL", "INDIANB", 10, None)),
    ("INDIANB 30 @ 842.50", None, ("BUY", "INDIANB", 30, 842.50)),
    ("INDIANB 30 at Rs 1,842.5", None, ("BUY", "INDIANB", 30, 1842.5)),
    ("INDIANB 30 @₹842", None, ("BUY", "INDIANB", 30, 842.0)),
    ("30", BUY_ALERT, ("BUY", "INDIANB", 30, None)),
    ("30", EXIT_ALERT, ("SELL", "INDIANB", 30, None)),
    ("sold 30", REVIEW_ALERT, ("SELL", "INDIANB", 30, None)),
    ("30 @ 840", BUY_ALERT, ("BUY", "INDIANB", 30, 840.0)),
    ("M&M 5", None, ("BUY", "M&M", 5, None)),
    ("BAJAJ-AUTO 2", None, ("BUY", "BAJAJ-AUTO", 2, None)),
    ("3MINDIA 1", None, ("BUY", "3MINDIA", 1, None)),
    # An explicit symbol beats the alert being replied to.
    ("SBIN 10", BUY_ALERT, ("BUY", "SBIN", 10, None)),
])
def test_trades_parse(text, reply, expected):
    command = parse(text, reply)
    assert isinstance(command, Command), getattr(command, "reply", command)
    assert (command.side, command.symbol, command.quantity, command.price) == expected


@pytest.mark.parametrize("text, reply, fragment", [
    ("30", None, "Which stock"),
    ("30", REGIME_ALERT, "Which stock"),
    ("30", REVIEW_ALERT, "buy or sell"),
    ("INDIANB", None, "How many"),
    ("INDIANB 30 842", None, "more than one number"),
    ("INDIANB 2.5", None, "whole number"),
    ("INDIANB 0", None, "whole number"),
    ("INDIANB SBIN 10", None, "Which stock"),
    ("bought and sold INDIANB 10", None, "both a buy and a sell"),
    ("INDIANB 10 @ 0", None, "above zero"),
])
def test_unclear_messages_ask_rather_than_guess(text, reply, fragment):
    result = parse(text, reply)
    assert isinstance(result, Problem)
    assert fragment.lower() in result.reply.lower()


@pytest.mark.parametrize("text, kind", [
    ("undo", "undo"), ("UNDO", "undo"), ("/undo", "undo"),
    ("holdings", "holdings"), ("portfolio", "holdings"),
    ("help", "help"), ("/start", "help"), ("hello there", "help"), ("", "help"),
])
def test_keywords(text, kind):
    assert parse(text).kind == kind


# --- Processing, against a real database ------------------------------------------


@pytest.fixture
def world(tmp_path, monkeypatch):
    """An isolated database, a stubbed Telegram and a stubbed market."""
    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{tmp_path/'inbox.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "_engine", engine)
    db.metadata.create_all(engine)
    with db.connection() as conn:
        conn.execute(db.stocks.insert(), [{"symbol": "INDIANB", "name": "Indian Bank"},
                                          {"symbol": "SBIN", "name": "State Bank of India"}])

    state = {"updates": [], "sent": [], "price": 843.50}

    monkeypatch.setattr(inbox, "telegram_credentials", lambda: ("TOKEN123", OWNER))
    monkeypatch.setattr(inbox, "fetch_updates", lambda token: list(state["updates"]))

    def fake_send(body, **kwargs):
        state["sent"].append({"body": body, **kwargs})
        return True

    monkeypatch.setattr(inbox.telegram, "send", fake_send)

    from src.data import prices

    def fake_price_at(symbol, when):
        if symbol not in ("INDIANB", "SBIN") or state["price"] is None:
            return None
        return state["price"], f"last trade at {when:%d %b %H:%M}"

    monkeypatch.setattr(prices, "price_at", fake_price_at)

    from src import committee

    monkeypatch.setattr(committee, "latest_run", lambda symbol: state.get("run"))
    return state


_next_id = [1000]


def message(text, *, chat=OWNER, reply_to=None, at=datetime(2026, 9, 23, 6, 12, tzinfo=timezone.utc)):
    """A Telegram update. Default time is 11:42 IST on 23 Sep."""
    _next_id[0] += 1
    msg = {"message_id": _next_id[0], "date": int(at.timestamp()), "chat": {"id": int(chat)}, "text": text}
    if reply_to:
        msg["reply_to_message"] = {"message_id": 5, "text": reply_to}
    return {"update_id": _next_id[0], "message": msg}


def transactions():
    with db.connection() as conn:
        return [dict(r._mapping) for r in conn.execute(select(db.transactions)).fetchall()]


def run(world, *updates):
    world["updates"] = list(updates)
    return inbox.process_pending()


def test_a_buy_is_recorded_priced_and_confirmed(world):
    outcomes = run(world, message("INDIANB 30"))

    assert outcomes[0]["status"] == "recorded"
    [tx] = transactions()
    assert (tx["symbol"], tx["side"], tx["quantity"], tx["price"]) == ("INDIANB", "BUY", 30, 843.50)
    assert tx["source"] == "telegram"
    assert tx["order_id"].startswith("tg-")
    assert tx["trade_date"] == date(2026, 9, 23)
    assert tx["total_charges"] > 0                      # Groww schedule applied
    assert "estimated" in tx["notes"]

    [sent] = world["sent"]
    assert "Recorded (LONG-TERM): BUY 30 INDIANB" in sent["body"]
    assert "estimated" in sent["body"]
    assert sent["reply_to"] is not None                  # threaded under the owner's message


def test_a_new_position_gets_an_exit_doctrine_and_committee_context(world):
    world["run"] = {"id": 7, "run_at": db.now() - timedelta(days=2), "conviction": 64.0,
                    "sizing": {"recommendation": "BALANCED", "stop_price": 799.0}}
    run(world, message("INDIANB 30"))

    with db.connection() as conn:
        position = dict(conn.execute(select(db.positions)).first()._mapping)
    assert position["exit_doctrine_json"]
    assert position["conviction"] == 64.0
    assert position["committee_run_id"] == 7
    # The researched stop: 25% below the actual fill, not the committee's quote.
    assert position["stop_price"] == pytest.approx(843.50 * 0.75, abs=0.01)
    assert position["strategy"] == "long_term"


def test_stale_committee_verdicts_are_not_attached(world):
    world["run"] = {"id": 7, "run_at": db.now() - timedelta(days=90), "conviction": 64.0, "sizing": {}}
    run(world, message("INDIANB 30"))
    with db.connection() as conn:
        position = dict(conn.execute(select(db.positions)).first()._mapping)
    assert position["conviction"] is None
    assert position["exit_doctrine_json"]


def test_the_same_message_read_twice_records_once(world):
    update = message("INDIANB 30")
    run(world, update)
    second = run(world, update)          # e.g. the job and a page load racing

    assert second == []
    assert len(transactions()) == 1
    assert len(world["sent"]) == 1


def test_a_strangers_message_is_ignored_silently(world):
    outcomes = run(world, message("INDIANB 30", chat=STRANGER))

    assert outcomes[0]["status"] == "ignored"
    assert transactions() == []
    assert world["sent"] == []           # no reply that confirms the bot is live


def test_replying_to_an_exit_alert_records_a_sale(world):
    run(world, message("INDIANB 30"))
    run(world, message("10", reply_to=EXIT_ALERT))

    sale = [t for t in transactions() if t["side"] == "SELL"]
    assert len(sale) == 1 and sale[0]["quantity"] == 10
    assert pf.state_for(sale[0]["position_id"]).quantity == 20


def test_selling_more_than_held_is_refused(world):
    run(world, message("INDIANB 30"))
    outcomes = run(world, message("sold INDIANB 40"))

    assert outcomes[0]["status"] == "rejected"
    assert "You hold 30" in world["sent"][-1]["body"]
    assert "Nothing was recorded" in world["sent"][-1]["body"]
    assert len(transactions()) == 1


def test_selling_what_was_never_held_is_refused(world):
    outcomes = run(world, message("sold SBIN 5"))
    assert outcomes[0]["status"] == "rejected"
    assert transactions() == []


def test_no_market_price_means_ask_for_one(world):
    world["price"] = None
    outcomes = run(world, message("INDIANB 30"))
    assert outcomes[0]["status"] == "rejected"
    assert "@" in world["sent"][-1]["body"]
    assert transactions() == []


def test_a_given_price_is_used_exactly(world):
    run(world, message("INDIANB 30 @ 840.25"))
    [tx] = transactions()
    assert tx["price"] == 840.25
    assert "as given" in tx["notes"]


def test_an_unknown_symbol_is_refused(world):
    outcomes = run(world, message("NOTREAL 30 @ 100"))
    assert outcomes[0]["status"] == "rejected"
    assert "recognise" in world["sent"][-1]["body"]
    assert transactions() == []


def test_the_trade_date_is_the_ist_date_the_message_was_sent(world):
    # 20:00 UTC on the 23rd is 01:30 IST on the 24th.
    run(world, message("INDIANB 30", at=datetime(2026, 9, 23, 20, 0, tzinfo=timezone.utc)))
    assert transactions()[0]["trade_date"] == date(2026, 9, 24)


def test_undo_removes_the_trade_and_leaves_no_empty_position(world):
    run(world, message("INDIANB 30"))
    outcomes = run(world, message("undo"))

    assert outcomes[0]["status"] == "undone"
    assert transactions() == []
    with db.connection() as conn:
        assert conn.execute(select(db.positions)).fetchall() == []
    assert "Undone" in world["sent"][-1]["body"]

    again = run(world, message("undo"))
    assert again[0]["status"] == "rejected"


def test_undo_walks_back_one_trade_at_a_time(world):
    run(world, message("INDIANB 30"))
    run(world, message("INDIANB 10"))
    run(world, message("undo"))
    [tx] = transactions()
    assert tx["quantity"] == 30


def test_undo_never_touches_a_trade_entered_elsewhere(world):
    pf.record_trade("SBIN", "BUY", date(2026, 9, 1), 5, 800.0, source="manual")
    outcomes = run(world, message("undo"))
    assert outcomes[0]["status"] == "rejected"
    assert len(transactions()) == 1


def test_undoing_a_closing_sale_removes_its_closed_trade_record(world):
    run(world, message("INDIANB 30"))
    run(world, message("sold INDIANB 30"))
    with db.connection() as conn:
        assert len(conn.execute(select(db.trades)).fetchall()) == 1

    run(world, message("undo"))
    with db.connection() as conn:
        assert conn.execute(select(db.trades)).fetchall() == []
    assert pf.find_open_position("INDIANB") is not None


def test_holdings_reply(world):
    run(world, message("INDIANB 30"))
    run(world, message("holdings"))
    assert "INDIANB (LONG-TERM): 30 shares" in world["sent"][-1]["body"]


def test_every_message_is_logged_in_the_inbox(world):
    run(world, message("INDIANB 30"), message("gibberish words"), message("x", chat=STRANGER))
    with db.connection() as conn:
        statuses = [r.status for r in conn.execute(select(db.telegram_inbox.c.status)).fetchall()]
    assert sorted(statuses) == ["answered", "ignored", "recorded"]


def test_an_unexpected_failure_is_reported_not_swallowed(world, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("database fell over")

    monkeypatch.setattr(inbox, "execute", boom)
    outcomes = run(world, message("INDIANB 30"))
    assert outcomes[0]["status"] == "error"
    assert "Check the Portfolio page" in world["sent"][-1]["body"]


def test_the_token_is_redacted_from_errors():
    assert "TOKEN123" not in inbox._redact("GET https://api.telegram.org/botTOKEN123/getUpdates", "TOKEN123")


def test_nothing_happens_without_credentials(world, monkeypatch):
    monkeypatch.setattr(inbox, "telegram_credentials", lambda: (None, None))
    world["updates"] = [message("INDIANB 30")]
    assert inbox.process_pending() == []
    assert transactions() == []


# --- The broker import replaces the estimate --------------------------------------


def test_a_groww_import_replaces_the_telegram_estimate(world, monkeypatch):
    from src.importers import groww

    monkeypatch.setattr(groww, "build_symbol_index", lambda: {"indian bank": "INDIANB", "indianb": "INDIANB"})
    # Messaged the day after buying: the dates differ by one.
    run(world, message("INDIANB 30", at=datetime(2026, 9, 24, 4, 0, tzinfo=timezone.utc)))

    frame = pd.DataFrame([{"Stock Name": "Indian Bank", "Type": "Buy", "Quantity": 30,
                           "Price": 841.25, "Order Date": "23-09-2026", "Order ID": "GRW900",
                           "Exchange": "NSE"}])
    preview = groww.build_preview(frame)
    assert len(preview.replacements) == 1
    assert preview.rows[0].to_display()["Status"] == "replaces Telegram entry"

    result = groww.commit(preview)
    assert result["replaced_telegram"] == 1

    [tx] = transactions()                                  # replaced, not doubled
    assert tx["price"] == 841.25
    assert tx["order_id"] == "GRW900"
    assert tx["source"] == "import"
    assert tx["trade_date"] == date(2026, 9, 23)
    assert pf.state_for(tx["position_id"]).quantity == 30

    # Once replaced, Telegram's undo leaves it alone.
    assert run(world, message("undo"))[0]["status"] == "rejected"
    assert len(transactions()) == 1


def test_two_equal_purchases_are_not_collapsed_into_one(world, monkeypatch):
    from src.importers import groww

    monkeypatch.setattr(groww, "build_symbol_index", lambda: {"indian bank": "INDIANB"})
    run(world, message("INDIANB 10", at=datetime(2026, 9, 22, 6, 0, tzinfo=timezone.utc)))
    run(world, message("INDIANB 10", at=datetime(2026, 9, 23, 6, 0, tzinfo=timezone.utc)))

    frame = pd.DataFrame([
        {"Stock Name": "Indian Bank", "Type": "Buy", "Quantity": 10, "Price": 830, "Order Date": "22-09-2026", "Order ID": "A1"},
        {"Stock Name": "Indian Bank", "Type": "Buy", "Quantity": 10, "Price": 845, "Order Date": "23-09-2026", "Order ID": "A2"},
    ])
    groww.commit(groww.build_preview(frame))

    prices_by_date = {t["trade_date"]: t["price"] for t in transactions()}
    assert prices_by_date == {date(2026, 9, 22): 830, date(2026, 9, 23): 845}


def test_an_unrelated_import_row_is_added_normally(world, monkeypatch):
    from src.importers import groww

    monkeypatch.setattr(groww, "build_symbol_index", lambda: {"indian bank": "INDIANB"})
    run(world, message("INDIANB 30"))
    frame = pd.DataFrame([{"Stock Name": "Indian Bank", "Type": "Buy", "Quantity": 5,
                           "Price": 800, "Order Date": "01-09-2026", "Order ID": "OLD1"}])
    preview = groww.build_preview(frame)
    assert preview.replacements == []
    groww.commit(preview)
    assert len(transactions()) == 2


# --- Pricing ---------------------------------------------------------------------


def _bars(times_ist, closes):
    index = pd.DatetimeIndex([pd.Timestamp(t) - pd.Timedelta(hours=5, minutes=30) for t in times_ist]).tz_localize("UTC")
    return pd.DataFrame({"Close": closes}, index=index.tz_convert("Asia/Kolkata"))


def test_price_at_uses_the_bar_at_or_before_the_message(monkeypatch):
    from src.data import prices

    bars = _bars(["2026-09-23 11:40", "2026-09-23 11:41", "2026-09-23 11:43"], [840.0, 841.0, 850.0])
    monkeypatch.setattr(prices, "_minute_bars", lambda symbol: bars)
    monkeypatch.setattr(prices, "_latest_quote", lambda symbol: pytest.fail("should not fall back"))

    price, how = prices.price_at("INDIANB", datetime(2026, 9, 23, 11, 42))
    assert price == 841.0
    assert "11:41" in how


def test_price_at_falls_back_to_the_quote(monkeypatch):
    from src.data import prices

    monkeypatch.setattr(prices, "_minute_bars", lambda symbol: None)
    monkeypatch.setattr(prices, "_latest_quote", lambda symbol: 855.5)
    assert prices.price_at("INDIANB", datetime(2026, 9, 23, 11, 42)) == (855.5, "latest quote")


def test_price_at_ignores_bars_from_after_the_message(monkeypatch):
    from src.data import prices

    monkeypatch.setattr(prices, "_minute_bars", lambda s: _bars(["2026-09-23 12:00"], [900.0]))
    monkeypatch.setattr(prices, "_latest_quote", lambda symbol: 855.5)
    assert prices.price_at("INDIANB", datetime(2026, 9, 23, 11, 42))[1] == "latest quote"


def test_price_at_returns_none_when_nothing_answers(monkeypatch):
    from src.data import prices

    def fail(symbol):
        raise ConnectionError("offline")

    monkeypatch.setattr(prices, "_minute_bars", fail)
    monkeypatch.setattr(prices, "_latest_quote", lambda symbol: None)
    assert prices.price_at("INDIANB", datetime(2026, 9, 23, 11, 42)) is None


# --- Two strategies -------------------------------------------------------------------


NEW_BUY_ALERT = "LONG-TERM BUY: INDIANB\nConviction 64/100"
SWING_BUY_ALERT = "SWING BUY: SBIN\nEntry around Rs 800"
LONG_TERM_EXIT = "LONG-TERM EXIT: INDIANB\nstop-loss"


@pytest.mark.parametrize("text, reply, expected", [
    ("30", NEW_BUY_ALERT, ("BUY", "INDIANB", 30, "long_term")),
    ("10", SWING_BUY_ALERT, ("BUY", "SBIN", 10, "swing")),
    ("10", LONG_TERM_EXIT, ("SELL", "INDIANB", 10, "long_term")),
    ("swing INDIANB 30", None, ("BUY", "INDIANB", 30, "swing")),
    ("bought INDIANB 30 swing", None, ("BUY", "INDIANB", 30, "swing")),
    ("INDIANB 30 long term", None, ("BUY", "INDIANB", 30, "long_term")),
    ("INDIANB 30", None, ("BUY", "INDIANB", 30, "long_term")),
    ("sold LT 10", None, ("SELL", "LT", 10, "long_term")),    # LT is a stock, not "long-term"
    ("30", "BUY candidate: INDIANB\nold alert", ("BUY", "INDIANB", 30, "long_term")),
])
def test_strategy_comes_from_the_alert_or_the_words(text, reply, expected):
    command = parse(text, reply)
    assert isinstance(command, Command), getattr(command, "reply", command)
    assert (command.side, command.symbol, command.quantity, command.strategy) == expected


def test_a_swing_trade_is_recorded_as_swing(world):
    run(world, message("swing SBIN 10"))
    with db.connection() as conn:
        position = dict(conn.execute(select(db.positions)).first()._mapping)
    assert position["strategy"] == "swing"
    assert position["exit_doctrine_json"] is None       # no long-term doctrine on a swing trade
    assert "Recorded (SWING)" in world["sent"][-1]["body"]


def test_one_stock_cannot_be_in_both_strategies(world):
    run(world, message("INDIANB 30"))
    outcomes = run(world, message("swing INDIANB 10"))
    assert outcomes[0]["status"] == "rejected"
    assert "FIFO" in world["sent"][-1]["body"]
    assert len(transactions()) == 1


def test_a_sale_goes_to_the_strategy_that_holds_the_stock(world):
    run(world, message("swing SBIN 10"))
    run(world, message("sold SBIN 10"))              # no strategy named
    with db.connection() as conn:
        rows = conn.execute(select(db.transactions.c.side, db.positions.c.strategy)
                            .join(db.positions, db.positions.c.id == db.transactions.c.position_id)).fetchall()
    assert {(r.side, r.strategy) for r in rows} == {("BUY", "swing"), ("SELL", "swing")}
