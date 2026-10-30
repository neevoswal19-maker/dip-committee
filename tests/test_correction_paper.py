"""Correction buying on paper: a correction from trigger to finished basket."""

from __future__ import annotations

import pandas as pd
import pytest
from sqlalchemy import create_engine, select

from src import correction_paper, db
from src.alerts import telegram
from src.config import load_config

SETTINGS = {"enabled": True, "mode": "paper", "index": "nifty500", "universe": "NIFTY 100", "depth_pct": 5.0,
            "window_sessions": 63, "max_hold_sessions": 126, "basket_capital": 500000.0, "slippage": 0.0005,
            "trigger_grace_sessions": 3}


class Cfg:
    """The real config, with the correction section replaced."""

    def __init__(self, **over):
        self.base, self.over = load_config(), {**SETTINGS, **over}

    def get(self, key, default=None):
        return self.over if key == "correction" else self.base.get(key, default)


@pytest.fixture
def sent(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_engine", create_engine(f"sqlite:///{tmp_path/'paper.db'}"))
    db.init_db()
    out: list[str] = []
    monkeypatch.setattr(telegram, "send", lambda body, **kw: (out.append(body), True)[1])
    return out


def _bars(values, start="2025-01-01"):
    c = pd.Series(values, index=pd.bdate_range(start, periods=len(values)), dtype=float)
    return pd.DataFrame({"open": c, "high": c, "low": c, "close": c})


def _market(after_index=(), after_fell=(), after_held=()):
    """300 flat sessions, then a day the index drops 6%, FELL 15% and HELD 2%."""
    idx = _bars([1000.0] * 300 + [940.0] + list(after_index))
    frames = {"FELL": _bars([100.0] * 300 + [85.0] + list(after_fell)),
              "HELD": _bars([100.0] * 300 + [98.0] + list(after_held))}
    return idx, frames


def _trades():
    with db.connection() as conn:
        return conn.execute(select(db.correction_trades)).mappings().all()


def test_a_correction_alerts_once_with_the_stocks_that_fell_more(sent):
    data = _market()
    assert correction_paper.run(Cfg(), data=data) == 1
    assert "PAPER BUY" in sent[0] and "FELL" in sent[0] and "HELD" not in sent[0]
    assert "target Rs 100.00 (+17.6%)" in sent[0]
    assert [(t["symbol"], t["entry"], t["closed"]) for t in _trades()] == [("FELL", None, False)]

    assert correction_paper.run(Cfg(), data=data) == 0                 # the next morning: nothing new
    assert len(sent) == 1


def test_the_basket_fills_hits_its_target_and_is_scored(sent):
    correction_paper.run(Cfg(), data=_market())
    later = _market(after_index=[950.0, 980.0, 1010.0], after_fell=[86.0, 90.0, 101.0],
                    after_held=[98.0, 99.0, 100.0])
    correction_paper.run(Cfg(), data=later)

    trade = _trades()[0]
    assert trade["closed"] and trade["reason"] == "target"
    assert trade["entry"] == pytest.approx(86.0 * 1.0005)
    assert trade["exit"] == pytest.approx(101.0)                       # gapped over the target: the open
    assert trade["units"] == int(500000 // (86.0 * 1.0005))
    assert any("PAPER SELL" in m and "FELL" in m and "target reached" in m for m in sent)
    done = [m for m in sent if "BASKET DONE" in m]
    assert len(done) == 1 and "Nifty 500 trade +6.3%" in done[0]       # 950 at the open -> 1010

    correction_paper.run(Cfg(), data=later)                            # scored once only
    assert len([m for m in sent if "BASKET DONE" in m]) == 1


def test_six_months_without_the_target_sells_at_the_close(sent):
    correction_paper.run(Cfg(max_hold_sessions=3), data=_market())
    correction_paper.run(Cfg(max_hold_sessions=3), data=_market(after_index=[945.0] * 3,
                                                                after_fell=[86.0, 87.0, 88.0],
                                                                after_held=[98.0] * 3))
    trade = _trades()[0]
    assert trade["reason"] == "time" and trade["exit"] == pytest.approx(88.0 * 0.9995)


def test_a_correction_from_before_the_start_is_not_backfilled(sent):
    idx, frames = _market(after_index=[940.0] * 5)                     # triggered six sessions ago
    assert correction_paper.run(Cfg(), data=(idx, frames)) == 0
    assert _trades() == []


def test_a_silent_run_records_nothing(sent):
    correction_paper.run(Cfg(), record=False, data=_market())
    assert not correction_paper.recorded(pd.Timestamp(_market()[0].index[300]).date())
    assert _trades() == []


def test_switched_off_does_nothing(sent):
    assert correction_paper.run(Cfg(enabled=False), data=_market()) == 0
    assert sent == []


def test_a_long_basket_is_split_across_messages():
    from src.strategy import correction as cr

    ep = cr.Episode(5, pd.Timestamp("2026-09-01"), 1000.0, pd.Timestamp("2026-09-15"), 944.0)
    picks = [{"symbol": f"STOCK{i:03d}LONGNAME", "target": 1234.56, "upside_pct": 12.3} for i in range(200)]
    messages = telegram.correction_buy(ep, picks, SETTINGS, ep.trigger_date.date())
    assert len(messages) > 1 and all(len(m) <= telegram.MAX_LENGTH for m in messages)
    assert sum(m.count("STOCK") for m in messages) == 200
