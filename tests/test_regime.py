"""Tests for the market regime classifier.

The one property that matters above the rest: a regime computed for a past
date must not change when later bars arrive. If it did, the backtest would be
using the future and the filter's measured value would be fiction.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from src.strategy import regime


def _index(values, start="2020-01-01"):
    stamps = pd.bdate_range(start, periods=len(values))
    return pd.DataFrame({"close": np.asarray(values, dtype=float)}, index=stamps)


def test_a_steady_rise_is_an_uptrend():
    frame = _index(100 * 1.0008 ** np.arange(600))
    assert regime.classify(frame).label == regime.UPTREND


def test_a_steady_fall_is_a_downtrend():
    frame = _index(100 * 0.9992 ** np.arange(600))
    result = regime.classify(frame)
    assert result.label == regime.DOWNTREND
    assert result.pct_vs_dma < 0
    assert result.dma_slope_pct < 0


def test_a_sharp_recovery_is_mixed_not_uptrend():
    """Price back above a still-falling average is a turn, not a trend."""
    falling = 100 * 0.999 ** np.arange(500)
    rebound = falling[-1] * 1.01 ** np.arange(1, 25)
    result = regime.classify(_index(np.concatenate([falling, rebound])))
    assert result.pct_vs_dma > 0
    assert result.dma_slope_pct < 0
    assert result.label == regime.MIXED


def test_short_history_is_unknown_not_a_trend():
    result = regime.classify(_index(np.linspace(100, 120, 250)))
    assert result.label == regime.UNKNOWN
    assert not result.known


def test_no_index_is_unknown():
    assert regime.classify(None).label == regime.UNKNOWN
    assert regime.classify(pd.DataFrame()).label == regime.UNKNOWN


def test_past_classification_cannot_see_the_future():
    """Append a crash after the query date; the answer for that date must hold."""
    rng = np.random.default_rng(1)
    history = 100 * np.cumprod(1 + rng.normal(0.0006, 0.01, 700))
    frame = _index(history)
    query = frame.index[600].date()

    before = regime.classify(frame, query)

    crash = history[-1] * np.cumprod(np.full(200, 0.99))
    extended = _index(np.concatenate([history, crash]))
    after = regime.classify(extended, query)

    assert before == after


def test_precomputed_frame_matches_direct_classification():
    rng = np.random.default_rng(2)
    frame = _index(100 * np.cumprod(1 + rng.normal(0.0003, 0.012, 800)))
    series = regime.regime_frame(frame)
    for i in (350, 500, 650, 799):
        when = frame.index[i].date()
        assert regime.classify(frame, when) == regime.classify(None, when, frame=series)


def test_classify_before_any_bar_is_unknown():
    frame = _index(np.linspace(100, 150, 600))
    assert regime.classify(frame, date(2000, 1, 1)).label == regime.UNKNOWN


def test_episodes_counts_separate_runs():
    labels = pd.Series(["UPTREND"] * 5 + ["DOWNTREND"] * 3 + ["UPTREND"] * 2
                       + ["UNKNOWN"] + ["DOWNTREND"] * 4)
    assert regime.episodes(labels) == {"UPTREND": 2, "DOWNTREND": 2}


def test_reason_names_the_numbers():
    result = regime.classify(_index(100 * 0.9992 ** np.arange(600)))
    assert "below" in result.reason and "falling" in result.reason
    assert result.to_dict()["label"] == regime.DOWNTREND


# --- Policy ---------------------------------------------------------------------

from src.agents import cmio  # noqa: E402
from src.config import load_config  # noqa: E402

from tests.test_agents import desks_all_at  # noqa: E402


class _Cfg:
    """The real config with a few keys overridden."""

    def __init__(self, **overrides):
        self._base = load_config()
        self._over = overrides

    def get(self, key, default=None):
        if key in self._over:
            return self._over[key]
        return self._base.get(key, default)


DOWN = regime.MarketRegime(regime.DOWNTREND, date(2026, 9, 23), pct_vs_dma=-3.0,
                           dma_slope_pct=-2.0, reason="test downtrend")
UP = regime.MarketRegime(regime.UPTREND, date(2026, 9, 23), pct_vs_dma=4.0,
                         dma_slope_pct=3.0, reason="test uptrend")
BLIND = regime.MarketRegime(regime.UNKNOWN, None, reason="index unavailable")


def _run(market, **overrides):
    return cmio.decide(
        "TEST", desks_all_at(5.0), price=100.0, atr=2.0, sector="Capital Goods",
        cfg=_Cfg(**overrides), market_regime=market,
    )


def test_default_policy_is_the_measured_one():
    assert load_config().get("regime.downtrend_action") == "inform"


def test_inform_changes_nothing_but_records_the_regime():
    baseline = _run(None)
    informed = _run(DOWN)
    assert informed.stance == baseline.stance == "BUY"
    assert informed.sizing["total_value"] == baseline.sizing["total_value"]
    assert informed.market_regime["label"] == regime.DOWNTREND
    assert informed.sizing["market_regime"]["label"] == regime.DOWNTREND
    assert "DOWNTREND" in informed.summary


def test_block_turns_a_buy_into_a_watch():
    """Conviction is unchanged; the market is what said wait."""
    report = _run(DOWN, **{"regime.downtrend_action": "block"})
    assert report.stance == "WATCH"
    assert report.sizing["is_buy"] is False
    assert report.sizing["total_value"] == 0
    assert report.conviction > 95
    assert any("block" in r for r in report.sizing["rejections"])


def test_reduce_scales_the_position():
    full = _run(None)
    halved = _run(DOWN, **{"regime.downtrend_action": "reduce", "regime.reduce_factor": 0.5})
    assert halved.stance == "BUY"
    assert 0 < halved.sizing["total_shares"] <= full.sizing["total_shares"] // 2
    assert halved.sizing["total_value"] < full.sizing["total_value"]
    tranche_shares = sum(t["shares"] for t in halved.sizing["tranches"])
    assert tranche_shares <= halved.sizing["total_shares"]


def test_unknown_regime_never_triggers_the_brake():
    """An index outage must not quietly cancel a position."""
    report = _run(BLIND, **{"regime.downtrend_action": "block"})
    assert report.stance == "BUY"
    assert report.sizing["is_buy"] is True
    assert "unknown" in report.summary.lower()


def test_uptrend_is_untouched_even_under_block():
    report = _run(UP, **{"regime.downtrend_action": "block"})
    assert report.stance == "BUY"
    assert report.sizing["is_buy"] is True


def test_a_typo_in_the_policy_falls_back_to_inform():
    assert regime.action(_Cfg(**{"regime.downtrend_action": "blokc"})) == regime.INFORM
    report = _run(DOWN, **{"regime.downtrend_action": "blokc"})
    assert report.stance == "BUY"
