"""ETF relative-strength swing systems: entry rules, exits, sizing and costs."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from src.config import load_config
from src.strategy import etf_swing as es
from src.strategy.simulator import NEXT_OPEN, Bars, simulate

CFG = load_config()


def _uptrend_with_pullback():
    """A steady uptrend, a 4-day pullback to the 20 EMA on light volume, then a trigger at bar 194."""
    closes = [100 + 0.2 * i for i in range(190)]
    closes += [137.0, 136.6, 136.3, 136.6, 137.6]                  # 190-193 pullback, 194 trigger
    closes += [137.6 + 0.2 * k for k in range(1, 6)]
    n = len(closes)
    index = pd.bdate_range("2020-01-01", periods=n)
    close = pd.Series(closes, index=index)
    volume = pd.Series(1000.0, index=index)
    volume.iloc[190:194] = 500.0
    frame = pd.DataFrame({"open": close - 0.1, "high": close + 0.5, "low": close - 0.5,
                          "close": close, "volume": volume})
    return frame


def _signals(frame, nifty=None, **kw):
    nifty = nifty if nifty is not None else pd.Series(100.0, index=frame.index)
    return es.signals(es.features(frame, nifty), **kw)


def test_the_textbook_setup_fires_once_on_the_trigger_bar():
    signal, rank = _signals(_uptrend_with_pullback())
    assert signal[194]
    assert signal.sum() == 1
    assert rank[194] < 0                                           # strongest RS first


def test_no_entry_when_the_nifty_regime_is_off():
    frame = _uptrend_with_pullback()
    signal, _ = _signals(frame, regime_ok=pd.Series(False, index=frame.index))
    assert not signal.any()


def test_no_entry_without_breadth():
    frame = _uptrend_with_pullback()
    signal, _ = _signals(frame, breadth_ok=pd.Series(False, index=frame.index))
    assert not signal.any()


def test_no_entry_when_the_nifty_is_doing_better():
    frame = _uptrend_with_pullback()
    nifty = pd.Series(100.0 * (1.004 ** np.arange(len(frame))), index=frame.index)
    signal, _ = _signals(frame, nifty=nifty)
    assert not signal.any()


def test_a_pullback_on_heavy_volume_does_not_qualify():
    frame = _uptrend_with_pullback()
    frame.loc[frame.index[190:194], "volume"] = 3000.0
    signal, _ = _signals(frame)
    assert not signal.any()


def test_index_stand_ins_without_volume_skip_the_volume_check():
    frame = _uptrend_with_pullback().assign(volume=np.nan)
    signal, _ = _signals(frame)
    assert signal[194]


def test_breadth_counts_members_above_their_50_day_sma():
    index = pd.bdate_range("2020-01-01", periods=80)
    up = pd.DataFrame({"close": np.linspace(100, 140, 80)}, index=index)
    down = pd.DataFrame({"close": np.linspace(140, 100, 80)}, index=index)
    assert es.breadth({"a": up, "b": up, "c": up, "d": down}, index).iloc[-1]
    assert not es.breadth({"a": up, "b": up, "c": down, "d": down}, index).iloc[-1]


# --- Exits ----------------------------------------------------------------------------


def _run(path, *, partial=0.0, regime_ok=None):
    """Signal on bar 5, entry at bar 6's open of 100, ATR 2: stop 97, +2R at 106."""
    n = 30
    index = pd.bdate_range("2021-01-01", periods=n)
    frame = pd.DataFrame({"open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0,
                          "atr": 2.0, "ema20": 95.0}, index=index)
    for i, row in path.items():
        for column, value in row.items():
            frame.iloc[i, frame.columns.get_loc(column)] = value
    bars = {"X": Bars.from_frame("X", frame, extra=("atr", "ema20"))}
    signal = np.zeros(n, dtype=bool)
    signal[5] = True
    result = simulate(bars, {"X": signal}, {"X": np.zeros(n)},
                      lambda s: es.SwingRSPolicy(partial_fraction=partial, regime_ok=regime_ok),
                      strategy="t", start=index[0].date(), end=index[-1].date(), mode=NEXT_OPEN,
                      slippage=0.0, with_costs=False, size_fn=es.risk_sizer(5000, 140000))
    return result.trades[0]


def test_the_stop_sits_one_and_a_half_atr_below_entry():
    trade = _run({8: {"low": 96.0}})
    assert trade.exit_reason == "stop"
    assert trade.fills[-1].price == pytest.approx(97.0)


def test_metals_trail_after_2R_without_selling_and_drop_the_hard_stop():
    trade = _run({8: {"high": 107.0, "close": 105.0}, 9: {"low": 96.0, "close": 100.0},
                  10: {"close": 94.0}})
    sells = [f for f in trade.fills if f.side == "SELL"]
    assert len(sells) == 1                                         # no partial for 2A
    assert trade.exit_reason == "trail_ema20"                      # the old 97 stop no longer applies
    assert sells[0].when == date(2021, 1, 18)                      # next open after bar 10 (Fri 15th)


def test_sectors_sell_40_percent_at_2R_then_trail():
    trade = _run({8: {"high": 107.0, "close": 105.0}, 10: {"close": 94.0}}, partial=0.4)
    sells = [f for f in trade.fills if f.side == "SELL"]
    assert sells[0].reason == "partial_2R" and sells[0].price == pytest.approx(106.0)
    assert sells[0].quantity == int(trade.bought * 0.4)
    assert trade.exit_reason == "trail_ema20"


def test_a_close_just_under_the_ema_does_not_trigger_the_trail():
    trade = _run({8: {"high": 107.0, "close": 105.0}, 10: {"close": 94.7}})   # 94.7 > 95 x 0.995
    assert trade.exit_reason == "end_of_test"


def test_sector_positions_leave_when_the_regime_turns():
    switch = date(2021, 1, 12)
    trade = _run({}, partial=0.4, regime_ok=lambda day: day < switch)
    assert trade.exit_reason == "regime_switch"


# --- Sizing and costs -----------------------------------------------------------------


def test_size_is_risk_over_stop_distance_capped():
    index = pd.bdate_range("2021-01-01", periods=3)
    frame = pd.DataFrame({"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "atr": 4.0}, index=index)
    bars = Bars.from_frame("X", frame, extra=("atr",))
    assert es.risk_sizer(5000, 140000)("X", bars, 1, 100.0) == pytest.approx(5000 / 6.0 * 100.0)
    frame["atr"] = 1.0
    bars = Bars.from_frame("X", frame, extra=("atr",))
    assert es.risk_sizer(5000, 140000)("X", bars, 1, 100.0) == pytest.approx(140000)


def test_etf_costs_charge_no_stt_on_buys_and_a_sliver_on_sells():
    from src import charges

    cost = es.etf_costs(CFG)
    buy_share = charges.estimate("BUY", 1000, 100.0, cfg=CFG)
    assert cost("BUY", 1000, 100.0) == pytest.approx(buy_share.total - buy_share.stt)
    sell_share = charges.estimate("SELL", 1000, 100.0, cfg=CFG)
    assert cost("SELL", 1000, 100.0) == pytest.approx(sell_share.total - sell_share.stt + 1.0)


# --- Data repair ----------------------------------------------------------------------


def test_an_unadjusted_split_is_rescaled_out_of_earlier_prices():
    index = pd.bdate_range("2019-01-01", periods=6)
    close = pd.Series([3000.0, 3030.0, 3060.0, 30.9, 31.2, 31.5], index=index)     # a 1:100 split
    frame = pd.DataFrame({"open": close, "high": close, "low": close, "close": close,
                          "volume": 10.0}, index=index)
    fixed = es.repair_splits(frame)
    assert fixed["close"].iloc[2] == pytest.approx(3060.0 * 30.9 / 3060.0)
    assert (fixed["close"].pct_change().dropna().abs() < 0.05).all()
    assert fixed["volume"].iloc[0] == pytest.approx(10.0 * 3060.0 / 30.9)


def test_an_ordinary_bad_day_is_left_alone():
    index = pd.bdate_range("2020-03-01", periods=3)
    close = pd.Series([100.0, 70.0, 72.0], index=index)                              # a 30% crash
    frame = pd.DataFrame({"open": close, "high": close, "low": close, "close": close}, index=index)
    assert es.repair_splits(frame)["close"].tolist() == [100.0, 70.0, 72.0]


def test_the_composite_chains_returns_so_a_late_listing_does_not_jump():
    index = pd.bdate_range("2021-01-01", periods=4)

    def member(closes, idx=index):
        c = pd.Series(closes, index=idx)
        return pd.DataFrame({"open": c, "high": c * 1.01, "low": c * 0.99, "close": c})

    late = member([500.0, 510.0], index[2:])
    comp = es.composite({"a": member([100, 110, 121, 121.0]), "b": member([50, 55, 60.5, 60.5]), "c": late})
    assert comp["close"].iloc[1] == pytest.approx(110.0)                            # both +10%
    assert comp["close"].iloc[3] == pytest.approx(121.0 * (1 + (0 + 0 + 0.02) / 3))  # c joins with +2%
    assert (comp["high"] > comp["close"]).all()
