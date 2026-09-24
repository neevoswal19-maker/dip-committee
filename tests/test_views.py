"""The dashboard pages render.

Rendered headlessly against a fresh database, in CI - the development
machine cannot run pandas (Smart App Control) and the deployed dashboard is
behind a password, so this is the one place a page that crashes on load is
caught before the owner sees it. Only pages that need no network are here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine

from src import db


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path/'views.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "_engine", engine)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    db.init_db()
    return engine


def _render(page: str):
    from streamlit.testing.v1 import AppTest

    root = Path(__file__).resolve().parent.parent
    at = AppTest.from_file(str(root / page), default_timeout=60)
    at.run()
    return at


def test_learning_page_renders_on_an_empty_database(fresh_db):
    at = _render("views/learning.py")
    assert not at.exception, [e.value for e in at.exception]
    text = " ".join(m.value for m in at.markdown)
    assert "Desk weights" in text
    assert any("needs 30 recommendations" in i.value for i in at.info)


def test_learning_page_shows_versions_and_proposals(fresh_db):
    from src.config import load_config
    from src.learning import proposals, weights

    weights.seed(load_config())
    proposals.record([{"kind": "rule", "config_path": "exit.trailing_stop.trail_pct",
                       "current_value": "20.0", "proposed_value": "10.0",
                       "rationale": "test", "evidence_json": "{}"}])
    at = _render("views/learning.py")
    assert not at.exception, [e.value for e in at.exception]
    labels = [b.label for b in at.button]
    assert "Approve" in labels and "Reject" in labels


def test_portfolio_page_renders_with_no_positions(fresh_db):
    at = _render("views/portfolio.py")
    assert not at.exception, [e.value for e in at.exception]
