"""The option-1 backtest's delivery replay: built from bhavcopies, point-in-time,
and the same thresholds as the live screen."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from src.config import load_config
from src.strategy import delivery_research as dr
from src.strategy.simulator import Bars

CFG = load_config()


def _bhavcopy(day, rows):
    return pd.DataFrame([{"SYMBOL": s, "SERIES": series, "CLOSE_PRICE": c, "DELIV_PER": d,
                          "TURNOVER_LACS": t, "trade_date": pd.Timestamp(day)} for s, series, c, d, t in rows])


def test_frames_keep_equity_rows_of_the_universe_only():
    days = [_bhavcopy("2024-01-01", [("AAA", "EQ", 100, 50, 900), ("AAA", "BE", 99, 10, 1),
                                     ("ZZZ", "EQ", 5, 30, 10)]),
            _bhavcopy("2024-01-02", [("AAA", "EQ", 101, 55, 950)])]
    frames = dr.delivery_frames(iter(days), {"AAA"})
    assert list(frames) == ["AAA"]
    assert list(frames["AAA"]["close"]) == [100, 101]
    assert list(frames["AAA"]["delivery_pct"]) == [50, 55]


def _tape(n=80, heavy_from=None, delivery=50.0, turnover=900.0):
    index = pd.bdate_range("2024-01-01", periods=n)
    close = pd.Series(100.0, index=index)
    close[1::2] = 99.0                                       # alternating down days
    pct = pd.Series(delivery, index=index)
    if heavy_from is not None:
        down = (close.diff() < 0).to_numpy()
        pct[(np.arange(n) >= heavy_from) & down] = delivery * 1.4
    return pd.DataFrame({"close": close, "delivery_pct": pct, "turnover_lacs": turnover})


def test_steady_delivery_does_not_confirm():
    assert not dr.delivery_confirmed(_tape(), CFG).any()


def test_rising_delivery_on_down_days_confirms():
    mask = dr.delivery_confirmed(_tape(heavy_from=50), CFG)
    assert not mask.iloc[:50].any()
    assert mask.iloc[60:].all()


def test_the_forty_percent_floor_and_turnover_floor_still_bind():
    assert not dr.delivery_confirmed(_tape(heavy_from=50, delivery=20.0), CFG).any()     # traded, not owned
    assert not dr.delivery_confirmed(_tape(heavy_from=50, turnover=100.0), CFG).any()    # Rs 1 cr/day


def test_the_replay_never_looks_ahead():
    tape = _tape(heavy_from=50)
    past = dr.delivery_confirmed(tape.iloc[:55], CFG)
    full = dr.delivery_confirmed(tape, CFG)
    assert (past == full.iloc[:55]).all()


def test_no_delivery_row_is_never_a_pass():
    index = pd.bdate_range("2024-01-01", periods=80)
    frame = pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0}, index=index)
    bars = {"AAA": Bars.from_frame("AAA", frame), "BBB": Bars.from_frame("BBB", frame)}
    signals = {"AAA": np.ones(80, dtype=bool), "BBB": np.ones(80, dtype=bool)}
    out = dr.confirm(signals, bars, {"AAA": _tape(heavy_from=50)}, CFG)
    assert not out["BBB"].any()                              # no bhavcopy history at all
    assert not out["AAA"][:50].any() and out["AAA"][60:].all()


def test_entries_are_counted_by_year():
    rows = [{"entry_date": date(2024, 3, 1)}, {"entry_date": date(2024, 9, 1)},
            {"entry_date": date(2025, 1, 2)}, {"entry_date": None}]
    assert dr.entries_per_year(rows) == {2024: 2, 2025: 1}
