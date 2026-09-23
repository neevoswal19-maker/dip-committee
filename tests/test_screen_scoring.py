"""Tests pinning the revised ranking function to the evidence behind it.

The previous weights survived a full rewrite without breaking a single test,
which is how a scoring function drifts away from what it was measured to do.
These tests assert the *directional* properties the measurement established,
not the exact constants - so the weights can still be tuned, but not silently
inverted.
"""

from __future__ import annotations

import pytest

from src.config import load_config
from src.screener import score_candidate


@pytest.fixture(scope="module")
def cfg():
    return load_config()


def _metrics(**overrides):
    base = {
        "drawdown_pct": 20.0,
        "pct_vs_dma_long": 0.0,
        "rsi": 35.0,
        "dma_slope_pct": 5.0,
        "down_day_delivery_ratio_avg10": 1.10,
        "delivery_persistence": 3,
        "near_support": False,
    }
    base.update(overrides)
    return base


def test_shallower_dips_score_higher(cfg):
    """Measured: deeper drawdowns did worse, cross-sectional IC -0.032 at 126d.

    The old function peaked at the middle of the band, which is the shape this
    asserts is gone.
    """
    shallow = score_candidate(_metrics(drawdown_pct=12.0), cfg)
    middle = score_candidate(_metrics(drawdown_pct=22.5), cfg)
    deep = score_candidate(_metrics(drawdown_pct=33.0), cfg)

    assert shallow > middle > deep


def test_drawdown_is_monotone_not_humped(cfg):
    scores = [score_candidate(_metrics(drawdown_pct=d), cfg) for d in range(10, 36, 2)]
    assert scores == sorted(scores, reverse=True)


def test_distance_above_the_long_dma_is_rewarded(cfg):
    """The one price signal that survived every correction (+0.085 at 126d)."""
    below = score_candidate(_metrics(pct_vs_dma_long=-8.0), cfg)
    at = score_candidate(_metrics(pct_vs_dma_long=0.0), cfg)
    above = score_candidate(_metrics(pct_vs_dma_long=9.0), cfg)

    assert below < at < above


def test_distance_to_dma_outweighs_dip_depth(cfg):
    """It measured better, so it must carry more weight than depth does."""
    depth_swing = (
        score_candidate(_metrics(drawdown_pct=10.0), cfg)
        - score_candidate(_metrics(drawdown_pct=35.0), cfg)
    )
    distance_swing = (
        score_candidate(_metrics(pct_vs_dma_long=10.0), cfg)
        - score_candidate(_metrics(pct_vs_dma_long=-10.0), cfg)
    )
    assert distance_swing > depth_swing


def test_rsi_carries_only_token_weight(cfg):
    """IC never cleared +0.019. It must not be able to dominate the ranking."""
    swing = (
        score_candidate(_metrics(rsi=20.0), cfg)
        - score_candidate(_metrics(rsi=40.0), cfg)
    )
    assert swing <= 6.0


def test_delivery_still_carries_real_weight(cfg):
    """Unmeasured is not disproven - the India-specific edge keeps its say."""
    swing = (
        score_candidate(_metrics(down_day_delivery_ratio_avg10=1.5, delivery_persistence=5), cfg)
        - score_candidate(_metrics(down_day_delivery_ratio_avg10=1.0, delivery_persistence=0), cfg)
    )
    assert swing >= 30.0


def test_atr_is_not_scored(cfg):
    """The regime split showed ATR was measuring beta: +0.108 up, -0.053 down.

    If someone adds it back, this fails.
    """
    without = score_candidate(_metrics(), cfg)
    with_atr = score_candidate(_metrics(atr_pct=9.0), cfg)
    assert without == with_atr


def test_score_stays_within_bounds(cfg):
    best = score_candidate(
        _metrics(drawdown_pct=10.0, pct_vs_dma_long=25.0, rsi=10.0, dma_slope_pct=30.0,
                 down_day_delivery_ratio_avg10=2.0, delivery_persistence=10, near_support=True),
        cfg,
    )
    worst = score_candidate(
        _metrics(drawdown_pct=35.0, pct_vs_dma_long=-30.0, rsi=40.0, dma_slope_pct=-5.0,
                 down_day_delivery_ratio_avg10=0.9, delivery_persistence=0),
        cfg,
    )
    assert best == 100.0
    assert worst == 0.0


def test_missing_metrics_do_not_crash_or_inflate(cfg):
    """A blind metric must contribute nothing, never a default."""
    assert score_candidate({}, cfg) == 0.0
    partial = score_candidate({"pct_vs_dma_long": 10.0}, cfg)
    assert 0.0 < partial <= 30.0
