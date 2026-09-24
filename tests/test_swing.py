"""The swing rules: that each fires where it should, not where it shouldn't,
and never on information from the future."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.strategy import swing


def _frame(closes, *, spread=0.01, opens=None):
    closes = np.asarray(closes, dtype=float)
    index = pd.bdate_range("2020-01-01", periods=len(closes))
    opens = np.asarray(opens, dtype=float) if opens is not None else closes
    return pd.DataFrame({
        "open": opens,
        "high": np.maximum(opens, closes) * (1 + spread),
        "low": np.minimum(opens, closes) * (1 - spread),
        "close": closes,
        "volume": 1_000_000,
    }, index=index)


def _uptrend(n=260, step=1.003):
    return list(100 * step ** np.arange(n))


def test_features_never_use_the_future():
    rng = np.random.default_rng(4)
    closes = 100 * np.cumprod(1 + rng.normal(0.0005, 0.015, 420))
    full = swing.features(_frame(closes))
    early = swing.features(_frame(closes[:300]))
    pd.testing.assert_frame_equal(full.iloc[:300], early, check_freq=False)


def test_rsi2_fires_after_a_sharp_pullback_in_an_uptrend():
    closes = _uptrend() + [None] * 0
    closes = closes + [closes[-1] * 0.98, closes[-1] * 0.98 ** 2, closes[-1] * 0.98 ** 3]
    f = swing.features(_frame(closes))
    entries = swing.RULES["rsi2"].entries(f)
    assert f["rsi2"].iloc[-1] < 10
    assert f["uptrend"].iloc[-1]
    assert entries.iloc[-1]


def test_rsi2_ignores_a_mild_dip():
    closes = _uptrend()
    closes = closes + [closes[-1] * 0.995]
    f = swing.features(_frame(closes))
    assert not swing.RULES["rsi2"].entries(f).iloc[-1]


def test_nothing_fires_below_the_200_day_average():
    closes = list(200 * 0.997 ** np.arange(260))                  # a downtrend
    closes = closes + [closes[-1] * 0.98, closes[-1] * 0.98 ** 2, closes[-1] * 0.98 ** 3]
    f = swing.features(_frame(closes))
    for key, rule in swing.RULES.items():
        if rule.cross_sectional:
            continue
        assert not rule.entries(f).iloc[-1], key


def test_double_sevens_enter_at_the_seven_day_low_and_exit_at_the_high():
    closes = _uptrend()
    closes = closes + [closes[-1] * (1 - 0.01 * k) for k in range(1, 4)]
    f = swing.features(_frame(closes))
    rule = swing.RULES["double7"]
    assert rule.entries(f).iloc[-1]

    recovered = closes + [closes[-1] * 1.08]
    f2 = swing.features(_frame(recovered))
    assert rule.exits(f2).iloc[-1]


def test_ibs_needs_a_close_near_the_low_and_below_yesterday():
    closes = _uptrend()
    frame = _frame(closes)
    last = frame.index[-1]
    prev_low = frame["low"].iloc[-2]
    frame.loc[last, ["high", "low", "close", "open"]] = [prev_low * 1.01, prev_low * 0.97,
                                                         prev_low * 0.975, prev_low * 1.0]
    f = swing.features(frame)
    assert f["ibs"].iloc[-1] < 0.2
    assert swing.RULES["ibs"].entries(f).iloc[-1]


def test_losers_pick_the_weakest_uptrending_stocks_on_rebalance_days_only():
    frames = {}
    for k in range(8):
        closes = _uptrend()
        drop = 1 - 0.01 * k                      # stock k fell k% over the last day
        closes = closes[:-1] + [closes[-2] * drop]
        frames[f"S{k}"] = swing.features(_frame(closes))
    picks = swing.losers_entries(frames, every=1, count=3)
    chosen = {s for s, series in picks.items() if series.iloc[-1]}
    assert chosen == {"S7", "S6", "S5"}

    weekly = swing.losers_entries(frames, every=5, count=3)
    fired_days = {d for series in weekly.values() for d in series[series].index}
    calendar = sorted(frames["S0"].index)
    assert fired_days <= set(calendar[::5])


def test_dividend_adjustment_leaves_the_latest_price_alone():
    frame = _frame([100.0, 100.0, 95.0, 96.0])
    frame["adj_close"] = [95.0, 95.0, 95.0, 96.0]          # a Rs 5 dividend before bar 2
    adjusted = swing.adjust_for_dividends(frame)
    assert adjusted["close"].iloc[-1] == 96.0
    assert adjusted["close"].iloc[0] == pytest.approx(95.0)
    assert adjusted["high"].iloc[0] == pytest.approx(frame["high"].iloc[0] * 0.95)


def test_the_plan_grid_is_nine_distinct_plans():
    grid = swing.plan_grid(swing.RULES["rsi2"])
    assert len(grid) == 9
    assert len({p.label for p in grid}) == 9
    assert all(p.max_hold == 10 for p in grid)


def test_plan_levels():
    plan = swing.ExitPlan(stop_atr=2.0, target_atr=1.0, max_hold=10)
    assert plan.levels(100.0, 3.0) == (94.0, 103.0)
    assert swing.ExitPlan(None, None, 10).levels(100.0, 3.0) == (None, None)
    assert plan.levels(100.0, 0.0) == (None, None)       # no ATR, no invented levels
