"""Correction buying: episodes, the old high, selection, exits and the verdict."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.strategy import correction as cr


def _series(levels):
    index = pd.bdate_range("2015-01-01", periods=len(levels))
    return pd.Series(levels, index=index, dtype=float)


def _path():
    """Up to 100, down to 75 (-25%), back to a new high of 101, then down 11%."""
    up = list(np.linspace(80, 100, 40))
    down = list(np.linspace(99, 75, 30))
    back = list(np.linspace(76, 101, 40))
    again = list(np.linspace(100.5, 89.5, 20))
    return _series(up + down + back + again)


def test_one_episode_per_fall_and_none_until_a_new_high():
    eps = cr.episodes(_path(), 10)
    assert len(eps) == 2                                  # the -25% fall triggers once, not three times
    first, second = eps
    assert first.peak_level == pytest.approx(100)
    assert first.trigger_level <= 90 and first.recovered_date is not None
    assert second.peak_level == pytest.approx(101) and second.recovered_date is None


def test_the_depth_decides_which_falls_count():
    path = _path()
    assert len(cr.episodes(path, 15)) == 1                # the second fall is only 11%
    assert len(cr.episodes(path, 20)) == 1
    assert cr.episodes(path, 20)[0].trigger_level <= 80


def test_episodes_never_look_ahead():
    path = _path()
    full = cr.episodes(path, 10)
    for n in range(50, len(path), 7):
        early = cr.episodes(path.iloc[:n], 10)
        assert [e.trigger_date for e in early] == [e.trigger_date for e in full if e.trigger_date < path.index[n]]


def test_the_old_high_is_taken_from_63_sessions_before_the_peak_to_the_trigger():
    close = _series([50.0] * 100 + [70.0] + [60.0] * 100 + [65.0] * 10)
    peak, trigger = close.index[180], close.index[205]
    assert cr.pre_correction_high(close, peak, trigger) == 60.0 + 5.0      # the 70 is 80 sessions too early
    assert cr.pre_correction_high(close, close.index[130], trigger) == 70.0


def _frame(closes, start="2013-01-01"):
    c = pd.Series(closes, index=pd.bdate_range(start, periods=len(closes)), dtype=float)
    return pd.DataFrame({"open": c, "high": c * 1.01, "low": c * 0.99, "close": c})


def test_only_stocks_that_fell_more_than_the_index_are_selected():
    n = cr.MIN_HISTORY + 100
    fell_more = _frame([100.0] * (n - 20) + [80.0] * 20)                                           # -20%
    fell_less = _frame([100.0] * (n - 20) + [95.0] * 20)                                           # -5%
    days = fell_more.index
    ep = cr.Episode(10, days[n - 60], 100.0, days[n - 10], 88.0)                                    # index -12%
    short = _frame([100.0] * 100 + [70.0] * 20, start=str(days[n - 115].date()))
    assert cr.selected(fell_more, ep)["target"] == pytest.approx(100.0)
    assert cr.selected(fell_less, ep) is None
    assert cr.selected(short, ep) is None                                                          # too little history


def _trade_frame(path):
    c = pd.Series(path, index=pd.bdate_range("2020-01-01", periods=len(path)), dtype=float)
    return pd.DataFrame({"open": c, "high": c, "low": c, "close": c})


def test_the_target_fills_at_the_old_high():
    f = _trade_frame([80, 80, 85, 90, 101, 99])
    t = cr.trade(f, "X", f.index[0], target=100.0, stop_pct=None, slippage=0)
    assert t.reason == "target" and t.exit == pytest.approx(101.0)      # gapped above: filled at the open
    assert t.entry_date == f.index[1] and t.reached


def test_the_stop_only_in_e2_and_the_two_year_limit():
    f = _trade_frame([80, 80, 70, 59, 58, 60])
    assert cr.trade(f, "X", f.index[0], 100.0, stop_pct=25.0, slippage=0).reason == "stop"
    held = cr.trade(f, "X", f.index[0], 100.0, stop_pct=None, slippage=0, max_hold=3)
    assert held.reason == "time" and held.sessions == 3
    still = cr.trade(f, "X", f.index[0], 100.0, stop_pct=None, slippage=0)
    assert still.reason == "open"                                       # data ended before two years


def test_the_index_benchmark_sells_when_the_index_regains_its_peak():
    close = _series([100, 90, 89, 95, 100.5, 102])
    ep = cr.Episode(10, close.index[0], 100.0, close.index[1], 90.0)
    out = cr.index_trade(close, close, ep)
    assert out["reached"] and out["return_pct"] == pytest.approx((100.5 / 89 - 1) * 100)


def test_the_verdict():
    good = [{"basket_pct": 20, "index_pct": 10, "trades": 30, "target_rate": 0.7}] * 5
    assert cr.judge(good)[0] is True
    few = good[:3]
    assert "only 3 completed episodes" in cr.judge(few)[1][0]
    behind = good[:3] + [{"basket_pct": 5, "index_pct": 10, "trades": 30, "target_rate": 0.7}] * 2
    ok, why = cr.judge(behind)
    assert not ok and any("3 of 5" in w for w in why)
    misses = [{"basket_pct": 20, "index_pct": 10, "trades": 30, "target_rate": 0.4}] * 5
    assert any("40%" in w for w in cr.judge(misses)[1])


# --- Short corrections ----------------------------------------------------------


def test_short_episodes_count_falls_below_an_old_peak():
    """Down 20% from an old peak, then a 6% dip and a rebound: the dip counts on its own."""
    levels = [100.0] * 10 + list(np.linspace(100, 80, 20)) + [80.0] * 60 + list(np.linspace(80, 75, 5)) + [79.0] * 20
    eps = cr.rolling_episodes(_series(levels), 5, window=63)
    assert len(eps) == 2
    assert eps[0].peak_level == pytest.approx(100)
    assert eps[1].peak_level == pytest.approx(80) and eps[1].trigger_level <= 76


def test_each_leg_of_a_staircase_fall_is_its_own_correction():
    stairs = _series([100.0] * 70 + [88.0] * 70 + [77.0] * 70 + [68.0] * 70)
    eps = cr.rolling_episodes(stairs, 10, window=63)
    assert [e.peak_level for e in eps] == [100.0, 88.0, 77.0]


def test_a_market_hovering_at_the_line_triggers_once():
    choppy = _series([100.0] * 63 + [94.8, 95.2, 94.8, 95.2, 94.8, 95.6, 94.7])
    assert len(cr.rolling_episodes(choppy, 5, window=63)) == 1


def test_the_first_check_waits_for_a_full_window():
    path = _series([100.0, 90.0] + [90.0] * 70)
    eps = cr.rolling_episodes(path, 5, window=63)
    assert len(eps) == 1 and eps[0].trigger_date == path.index[62]


def test_short_episodes_never_look_ahead():
    path = _series([100.0] * 70 + list(np.linspace(100, 85, 30)) + list(np.linspace(85, 102, 40)) + [92.0] * 20)
    full = cr.rolling_episodes(path, 5, window=63)
    for n in range(70, len(path), 5):
        early = cr.rolling_episodes(path.iloc[:n], 5, window=63)
        assert [e.trigger_date for e in early] == [e.trigger_date for e in full if e.trigger_date < path.index[n]]


def test_the_short_target_is_the_window_high():
    close = _series([120.0] + [100.0] * 70 + [90.0])
    assert cr.window_high(close, close.index[-1], 63) == 100.0      # the 120 is outside the window
    frame = _frame([100.0] * (cr.SHORT_MIN_HISTORY + 10) + [90.0])
    ep = cr.Episode(5, frame.index[-20], 100.0, frame.index[-1], 94.0)  # index -6%
    assert cr.selected_short(frame, ep, 63) == {"target": 100.0, "fall": pytest.approx(-0.10)}


def test_the_short_verdict_also_needs_to_beat_buying_everything():
    row = {"basket_pct": 6, "index_pct": 3, "trades": 30, "target_rate": 0.7, "survivors_pct": 8}
    ok, why = cr.judge([row] * 12, cr.SHORT_PASS)
    assert not ok and any("buying every stock" in w for w in why)
    assert cr.judge([{**row, "survivors_pct": 4}] * 12, cr.SHORT_PASS) == (True, [])
