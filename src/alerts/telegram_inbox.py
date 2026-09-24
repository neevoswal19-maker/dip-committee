"""Recording trades by messaging the bot.

The owner buys on Groww, then tells the bot - "INDIANB 30", or just "30" as a
reply to the alert that prompted it - and the trade lands in the portfolio.

Nothing listens continuously. Telegram holds unread messages for 24 hours and
hands them out through `getUpdates`, so `process_pending` is called from
three places: a GitHub Actions job every 15 minutes, the dashboard when a
page loads, and the evening scan before it checks holdings. Whichever runs
first does the work.

Three rules shape the code:

**Only the owner is listened to.** The repository is public and anyone can
message a bot. Messages from any chat other than `TELEGRAM_CHAT_ID` are
logged and ignored, without a reply that would confirm the bot is live.

**Claim, then act.** Each update is inserted into `telegram_inbox` before
anything is recorded, and `update_id` is unique - so two callers racing on
the same message cannot both record it. The transaction's order id is
`tg-<update_id>`, which the ledger's own unique constraint also rejects on
repeat.

**The message's timestamp is the trade's timestamp.** A message handled
fifteen minutes late is still dated, and priced, at the minute it was sent.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError

from src import db
from src.alerts import telegram
from src.config import load_config, telegram_credentials

log = logging.getLogger(__name__)

UPDATES_API = "https://api.telegram.org/bot{token}/getUpdates"

BUY_WORDS = {"buy", "bought", "b", "purchase", "purchased", "add", "added"}
SELL_WORDS = {"sell", "sold", "s", "exit", "exited", "trim", "trimmed"}
FILLER = {
    "shares", "share", "qty", "quantity", "of", "x", "units", "unit", "nos",
    "stock", "stocks", "i", "have", "just", "today", "rs", "inr", "the", "a",
    "an", "my", "for", "at", "market", "more", "some", "in", "trade", "position",
}
#: Words that say which strategy a trade belongs to.
SWING_WORDS = {"swing"}
# Not "lt": LT (Larsen & Toubro) is a Nifty 50 symbol.
LONG_TERM_WORDS = {"long-term", "longterm", "long"}   # "long term" splits into two
UNDO_WORDS = {"undo", "/undo"}
HOLDINGS_WORDS = {"holdings", "portfolio", "positions", "/holdings", "/portfolio"}
HELP_WORDS = {"help", "/help", "/start", "start", "?"}

#: The first line of an alert this system sent, as Telegram hands it back in
#: `reply_to_message.text` - plain text, the HTML already stripped.
REPLY_HEADER = re.compile(
    r"^\s*(?:(SWING|LONG-TERM)\s+)?"
    r"(BUY candidate|BUY|EXIT|TRIM|REVIEW|HOLD|TAX DEADLINE|Tax deadline)"
    r"\s*:\s*([A-Z0-9][A-Z0-9&\-]*)"
)
PRICE = re.compile(r"(?:@|\bat\b)\s*(?:rs\.?|inr|₹)?\s*([0-9][0-9,]*(?:\.[0-9]+)?)", re.I)
TOKEN = re.compile(r"\d+[A-Za-z][A-Za-z0-9&\-]*|[A-Za-z][A-Za-z0-9&\-]*|\d+(?:\.\d+)?")

HELP = (
    "Tell me about a trade and I'll record it in your portfolio.\n\n"
    "<b>INDIANB 30</b> - bought 30\n"
    "<b>sold INDIANB 30</b> - sold 30\n"
    "<b>30</b> as a reply to an alert - that alert's stock\n"
    "<b>INDIANB 30 @ 842.50</b> - with the exact price\n"
    "<b>swing INDIANB 30</b> - record it as a swing trade (the default is long-term)\n\n"
    "Without a price I use the market price at the minute you sent it, and "
    "your Groww import later replaces it with the real fill.\n\n"
    "<b>undo</b> - reverse the last trade recorded here\n"
    "<b>holdings</b> - what you hold"
)


# --- Parsing (pure) -------------------------------------------------------------


@dataclass
class Command:
    kind: str                       # trade | undo | holdings | help
    side: str | None = None         # BUY | SELL
    symbol: str | None = None
    quantity: int | None = None
    price: float | None = None      # set only when the owner typed one
    strategy: str = "long_term"     # long_term | swing


@dataclass
class Problem:
    """A message that could not be turned into an action, and what to say."""

    reply: str


def parse(text: str | None, reply_to_text: str | None = None) -> Command | Problem:
    """Turn a message into a command. No I/O, so every case is testable."""
    raw = (text or "").strip()
    lowered = raw.lower()

    if not raw or lowered in HELP_WORDS:
        return Command("help")
    if lowered in UNDO_WORDS:
        return Command("undo")
    if lowered in HOLDINGS_WORDS:
        return Command("holdings")

    price: float | None = None
    match = PRICE.search(raw)
    if match:
        price = float(match.group(1).replace(",", ""))
        if price <= 0:
            return Problem("The price has to be above zero.")
        raw = raw[: match.start()] + " " + raw[match.end():]

    side: str | None = None
    symbols: list[str] = []
    numbers: list[str] = []
    stated_strategy: str | None = None

    for token in TOKEN.findall(raw):
        word = token.lower()
        if word in SWING_WORDS:
            stated_strategy = "swing"
            continue
        if word in LONG_TERM_WORDS or word == "term":
            stated_strategy = stated_strategy or "long_term"
            continue
        if word in BUY_WORDS or word in SELL_WORDS:
            this = "BUY" if word in BUY_WORDS else "SELL"
            if side and side != this:
                return Problem("That reads as both a buy and a sell. Which was it?")
            side = this
        elif word in FILLER:
            continue
        elif token[0].isdigit() and not any(c.isalpha() for c in token):
            numbers.append(token)
        else:
            symbols.append(token.upper())

    if not numbers and not symbols:
        return Command("help")

    if not numbers:
        # "INDIANB" or "bought INDIANB" is a trade missing its quantity;
        # "hello there" is conversation, and gets the help text instead.
        looks_like_trade = side is not None or (
            len(symbols) == 1 and any(t == symbols[0] for t in TOKEN.findall(raw))
        )
        if not looks_like_trade:
            return Command("help")
        return Problem("How many shares? For example: <b>INDIANB 30</b>")
    if len(numbers) > 1:
        return Problem(
            "I see more than one number. Put <b>@</b> before the price, "
            "for example: <b>INDIANB 30 @ 842.50</b>"
        )
    if "." in numbers[0] or int(numbers[0]) <= 0:
        return Problem("The quantity has to be a whole number of shares above zero.")
    quantity = int(numbers[0])

    if len(symbols) > 1:
        return Problem(f"Which stock? I read {', '.join(symbols[:3])}.")

    replied_symbol, replied_side, replied_label, replied_strategy = None, None, None, None
    header = REPLY_HEADER.match(reply_to_text or "")
    if header:
        prefix, replied_label, replied_symbol = header.group(1), header.group(2), header.group(3)
        replied_side = {"BUY candidate": "BUY", "BUY": "BUY", "EXIT": "SELL",
                        "TRIM": "SELL"}.get(replied_label)
        replied_strategy = "swing" if prefix == "SWING" else "long_term"

    if symbols:
        symbol = symbols[0]
        if side is None:
            side = replied_side if symbol == replied_symbol and replied_side else "BUY"
    elif replied_symbol:
        symbol = replied_symbol
        if side is None:
            side = replied_side
        if side is None:
            return Problem(
                f"Did you buy or sell {symbol}? Reply <b>bought {quantity}</b> "
                f"or <b>sold {quantity}</b>."
            )
    else:
        return Problem(
            "Which stock? Reply to the alert with the quantity, or send "
            f"<b>INDIANB {quantity}</b>."
        )

    strategy = stated_strategy or (replied_strategy if symbol == replied_symbol else None) or "long_term"
    return Command("trade", side=side, symbol=symbol, quantity=quantity, price=price,
                   strategy=strategy)


# --- Acting --------------------------------------------------------------------


def _rs(value: float) -> str:
    return f"₹{value:,.2f}"


def _known_symbol(symbol: str) -> bool:
    with db.connection() as conn:
        for table in (db.stocks, db.positions):
            if conn.execute(
                select(table.c.symbol).where(table.c.symbol == symbol).limit(1)
            ).first():
                return True
    return False


def _position_context(symbol: str, price: float, trade_date: Any, cfg: Any) -> dict[str, Any]:
    """What a new position should carry, mirroring the dashboard's entry form.

    The committee's latest verdict supplies conviction and the stop, when it
    assessed this stock in the last month, and the exit doctrine is written
    at the moment of purchase - without one the evening job has nothing to
    check the holding against.
    """
    from src import committee as committee_module
    from src.strategy import exit as exit_rules

    context: dict[str, Any] = {}
    run = None
    try:
        run = committee_module.latest_run(symbol)
    except Exception as exc:
        log.info("No committee context for %s: %s", symbol, exc)

    stop = None
    if run and run.get("run_at") and db.now() - run["run_at"] <= timedelta(days=30):
        sizing = run.get("sizing") or {}
        stop = sizing.get("stop_price")
        context.update(
            conviction=run.get("conviction"),
            recommendation=sizing.get("recommendation"),
            committee_run_id=run.get("id"),
            stop_price=stop,
        )

    if str(cfg.get("sizing.stop_rule", "atr")).lower() == "pct":
        # The researched stop, from the actual fill rather than the committee's quote.
        stop = round(price * (1 - float(cfg.get("sizing.stop_pct", 25.0)) / 100.0), 2)
        context["stop_price"] = stop
    context["exit_doctrine"] = exit_rules.build_doctrine(
        symbol, price, cfg, stop_price=stop, entry_date=trade_date
    ).to_dict()
    return context


def _trade(command: Command, update_id: int, message_at: datetime, cfg: Any) -> tuple[str, int | None, str]:
    from src import portfolio as pf
    from src.data import prices

    symbol, side, quantity = command.symbol, command.side, command.quantity
    trade_date = message_at.date()

    if command.price is not None:
        if not _known_symbol(symbol) and prices.price_at(symbol, message_at) is None:
            return "rejected", None, f"I don't recognise <b>{telegram._escape(symbol)}</b> as an NSE symbol. Nothing was recorded."
        price, how = command.price, "the price you gave"
        estimated = False
    else:
        found = prices.price_at(symbol, message_at)
        if found is None:
            return "rejected", None, (
                f"I couldn't find a market price for <b>{telegram._escape(symbol)}</b>. "
                f"If the symbol is right, send it with the price: "
                f"<b>{telegram._escape(symbol)} {quantity} @ 842.50</b>. Nothing was recorded."
            )
        price, how = found
        estimated = True

    strategy = command.strategy
    label = pf.LABELS.get(strategy, strategy)
    if side == "SELL":
        # A sale goes to whichever strategy actually holds the stock, unless
        # the owner named one.
        held_in = pf.open_strategy_for(symbol)
        if held_in and strategy != held_in and command.strategy == "long_term":
            strategy, label = held_in, pf.LABELS.get(held_in, held_in)
    position_id = pf.find_open_position(symbol, strategy)
    created = False
    if side == "SELL" and position_id is None:
        return "rejected", None, (
            f"You don't hold any {telegram._escape(symbol)} as a {label} position, so there's "
            f"nothing to sell. Nothing was recorded."
        )
    if side == "BUY" and position_id is None:
        try:
            if strategy == "swing":
                position_id = pf.create_position(symbol, strategy="swing",
                                                 notes="Recorded from Telegram as a swing trade.")
            else:
                position_id = pf.create_position(symbol, **_position_context(symbol, price, trade_date, cfg))
        except pf.LedgerError as exc:
            return "rejected", None, f"{telegram._escape(str(exc))} Nothing was recorded."
        created = True

    note = f"From Telegram. Price {'estimated: ' + how if estimated else 'as given in the message'}."
    try:
        _, state = pf.record_trade(
            symbol, side, trade_date, quantity, price,
            position_id=position_id, order_id=f"tg-{update_id}",
            source="telegram", notes=note, cfg=cfg, strategy=strategy,
        )
    except pf.LedgerError as exc:
        if created:
            pf._remove_position_if_unreferenced(position_id)
        held = pf.state_for(position_id, cfg=cfg).quantity if side == "SELL" else 0
        if side == "SELL" and quantity > held:
            return "rejected", None, (
                f"You hold {held:g} {telegram._escape(symbol)}, so I can't record selling "
                f"{quantity}. Nothing was recorded."
            )
        return "rejected", None, f"Couldn't record that: {telegram._escape(str(exc))}. Nothing was recorded."

    with db.connection() as conn:
        transaction = conn.execute(
            select(db.transactions.c.id, db.transactions.c.total_charges)
            .where(db.transactions.c.order_id == f"tg-{update_id}")
        ).first()

    charges = float(transaction.total_charges or 0) if transaction else 0.0
    price_note = f"estimated, {how}" if estimated else "as you gave it"
    if state.is_open:
        position = f"Position: {state.quantity:g} shares, average cost {_rs(state.avg_cost)}."
    else:
        position = f"Position closed. Realised {_rs(state.realised_gain)} after charges."

    reply = (
        f"<b>Recorded ({label}): {side} {quantity} {telegram._escape(symbol)}</b> at {_rs(price)} "
        f"({telegram._escape(price_note)}), charges {_rs(charges)}.\n"
        f"{position}\n\n"
        f"Wrong? Reply <b>undo</b>. To fix just the price, undo and resend as "
        f"<b>{telegram._escape(symbol)} {quantity} @ price</b>."
    )
    return "recorded", (transaction.id if transaction else None), reply


def _undo(cfg: Any) -> tuple[str, int | None, str]:
    from src import portfolio as pf

    with db.connection() as conn:
        rows = conn.execute(
            select(db.telegram_inbox.c.id, db.telegram_inbox.c.transaction_id)
            .where(db.telegram_inbox.c.status == "recorded")
            .where(db.telegram_inbox.c.transaction_id.isnot(None))
            .order_by(desc(db.telegram_inbox.c.id))
            .limit(1)
        ).fetchall()

    if not rows:
        return "rejected", None, "There's nothing recorded from Telegram to undo."

    inbox_id, transaction_id = rows[0].id, rows[0].transaction_id
    with db.connection() as conn:
        transaction = conn.execute(
            select(db.transactions).where(db.transactions.c.id == transaction_id)
        ).first()

    if transaction is None:
        with db.connection() as conn:
            conn.execute(db.telegram_inbox.update().where(db.telegram_inbox.c.id == inbox_id).values(status="undone"))
        return "rejected", None, "That trade was already removed on the dashboard. Nothing to undo."

    record = dict(transaction._mapping)
    if record.get("source") != "telegram":
        return "rejected", None, (
            "Your Groww import has already replaced that trade with the real fill, so I "
            "won't undo it from here. Remove it on the Portfolio page if it's wrong."
        )

    pf.delete_transaction(transaction_id, cfg=cfg)
    with db.connection() as conn:
        conn.execute(db.telegram_inbox.update().where(db.telegram_inbox.c.id == inbox_id).values(status="undone"))

    symbol = record["symbol"]
    remaining = pf.find_open_position(symbol)
    held = pf.state_for(remaining, cfg=cfg).quantity if remaining else 0
    return "undone", transaction_id, (
        f"<b>Undone: {record['side']} {record['quantity']:g} {telegram._escape(symbol)}</b> at "
        f"{_rs(record['price'])}.\nYou now hold {held:g} {telegram._escape(symbol)}."
    )


def _holdings(cfg: Any) -> tuple[str, int | None, str]:
    from src import portfolio as pf

    states = pf.open_positions(cfg=cfg)
    if not states:
        return "answered", None, "No open positions are recorded."
    lines = ["<b>Holdings</b>"]
    for state in states:
        lines.append(
            f"{telegram._escape(state.symbol)} ({pf.LABELS.get(state.strategy, state.strategy)}): "
            f"{state.quantity:g} shares, average cost {_rs(state.avg_cost)}"
        )
    lines.append(f"\nInvested: {_rs(sum(s.invested for s in states))}")
    return "answered", None, "\n".join(lines)


def execute(parsed: Command | Problem, update_id: int, message_at: datetime, cfg: Any) -> tuple[str, int | None, str]:
    """Carry out a parsed message. Returns (status, transaction_id, reply)."""
    if isinstance(parsed, Problem):
        return "rejected", None, parsed.reply
    if parsed.kind == "help":
        return "answered", None, HELP
    if parsed.kind == "undo":
        return _undo(cfg)
    if parsed.kind == "holdings":
        return _holdings(cfg)
    return _trade(parsed, update_id, message_at, cfg)


# --- The loop -------------------------------------------------------------------


def _redact(text: str, token: str | None) -> str:
    """Never let the bot token reach a log or a page - it is in every API URL."""
    return text.replace(token, "<token>") if token else text


def fetch_updates(token: str) -> list[dict[str, Any]]:
    """Unread updates, oldest first, starting after the last one handled.

    Passing `offset` also tells Telegram to discard everything before it,
    which is safe because those are all claimed in `telegram_inbox`.
    """
    with db.connection() as conn:
        last = conn.execute(select(func.max(db.telegram_inbox.c.update_id))).scalar()

    params: dict[str, Any] = {"timeout": 0, "allowed_updates": json.dumps(["message"])}
    if last is not None:
        params["offset"] = int(last) + 1

    response = httpx.get(UPDATES_API.format(token=token), params=params, timeout=20.0)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"getUpdates refused: {payload.get('description')}")
    return sorted(payload.get("result") or [], key=lambda u: u["update_id"])


def _handle(update: dict[str, Any], owner_chat: str, cfg: Any, send_replies: bool) -> dict[str, Any] | None:
    update_id = int(update["update_id"])
    message = update.get("message") or {}
    chat = str((message.get("chat") or {}).get("id", ""))
    text = message.get("text")
    message_at = (
        datetime.fromtimestamp(int(message["date"]), db.IST).replace(tzinfo=None)
        if message.get("date") else db.now()
    )

    try:
        with db.connection() as conn:
            conn.execute(db.telegram_inbox.insert().values(
                update_id=update_id, received_at=db.now(), message_at=message_at,
                chat_id=chat, text=(text or "")[:1000], status="claimed",
            ))
    except IntegrityError:
        return None  # another caller has it

    def finish(status: str, transaction_id: int | None = None, reply: str | None = None) -> None:
        with db.connection() as conn:
            conn.execute(
                db.telegram_inbox.update()
                .where(db.telegram_inbox.c.update_id == update_id)
                .values(status=status, transaction_id=transaction_id, reply=reply)
            )

    if chat != str(owner_chat).strip():
        log.warning("Ignored a message from chat %s, which is not the owner's", chat)
        finish("ignored")
        return {"update_id": update_id, "status": "ignored"}

    if text is None:
        finish("ignored")
        return {"update_id": update_id, "status": "ignored"}

    try:
        reply_text = (message.get("reply_to_message") or {}).get("text")
        status, transaction_id, reply = execute(parse(text, reply_text), update_id, message_at, cfg)
    except Exception as exc:
        log.exception("Failed handling Telegram update %s", update_id)
        status, transaction_id = "error", None
        reply = (
            f"Something went wrong handling that ({type(exc).__name__}). Check the "
            f"Portfolio page before sending it again, in case it was recorded."
        )

    finish(status, transaction_id, reply)

    if send_replies and reply:
        telegram.send(
            reply, alert_type="inbox_reply", dedupe_key=f"inbox|{update_id}",
            cfg=cfg, reply_to=message.get("message_id"),
        )

    return {"update_id": update_id, "status": status, "transaction_id": transaction_id, "reply": reply}


def process_pending(cfg: Any = None, *, send_replies: bool = True) -> list[dict[str, Any]]:
    """Handle every unread message. Safe to call from anywhere, any number of times."""
    token, owner_chat = telegram_credentials()
    if not (token and owner_chat):
        return []

    cfg = cfg or load_config()
    db.init_db()

    try:
        updates = fetch_updates(token)
    except Exception as exc:
        log.warning("Could not fetch Telegram messages: %s", _redact(str(exc), token))
        return []

    outcomes = []
    for update in updates:
        outcome = _handle(update, owner_chat, cfg, send_replies)
        if outcome:
            outcomes.append(outcome)

    recorded = sum(1 for o in outcomes if o["status"] == "recorded")
    if outcomes:
        log.info("Telegram inbox: %d message(s), %d trade(s) recorded", len(outcomes), recorded)
    return outcomes
