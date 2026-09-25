"""Holding messages for the send time.

The morning job builds every alert before 08:00 and posts them at 08:00.
On the first morning, building them after the hold - dozens of queries to a
database in Singapore from a runner in the US - pushed the summary to 08:03.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select

from src import db
from src.alerts import telegram


@pytest.fixture
def posted(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path/'queue.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "_engine", engine)
    db.metadata.create_all(engine)
    monkeypatch.setattr(telegram, "telegram_credentials", lambda: ("TOKEN", "123"))
    monkeypatch.setattr(telegram, "_queue", None)

    calls: list[str] = []

    class Response:
        def raise_for_status(self):
            return None

    def fake_post(url, json, timeout):
        calls.append(json["text"])
        return Response()

    monkeypatch.setattr(telegram.httpx, "post", fake_post)
    return calls


def _delivered():
    with db.connection() as conn:
        return {r.dedupe_key: r.delivered for r in conn.execute(
            select(db.alerts_sent.c.dedupe_key, db.alerts_sent.c.delivered))}


def test_held_messages_are_claimed_now_and_posted_at_release(posted):
    telegram.hold_messages()
    assert telegram.send("first", dedupe_key="a") is True
    assert telegram.send("second", dedupe_key="b") is True
    assert posted == []                                   # nothing posted yet
    assert _delivered() == {"a": False, "b": False}       # but both claimed

    assert telegram.release_messages() == 2
    assert posted == ["first", "second"]                  # in order
    assert _delivered() == {"a": True, "b": True}


def test_a_duplicate_is_refused_while_queued(posted):
    telegram.hold_messages()
    assert telegram.send("summary", dedupe_key="summary|today") is True
    assert telegram.send("summary again", dedupe_key="summary|today") is False
    telegram.release_messages()
    assert posted == ["summary"]


def test_release_ends_the_hold(posted):
    telegram.hold_messages()
    telegram.release_messages()
    assert telegram.send("now", dedupe_key="c") is True
    assert posted == ["now"]


def test_without_a_hold_sending_is_immediate(posted):
    assert telegram.send("straight away", dedupe_key="d") is True
    assert posted == ["straight away"]
    assert telegram.release_messages() == 0
