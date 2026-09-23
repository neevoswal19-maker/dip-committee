"""Tests for the signal-validation machinery.

The point of this module is to tell us when a signal does not work, so a bug
here is worse than a bug almost anywhere else: it would let a dead signal keep
driving position sizes while appearing to have been checked.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from src.learning import attribution


def test_spearman_matches_a_hand_computed_value():
    # Ranks of y contain a tie, which is the case a naive implementation of
    # the 6*sum(d^2) shortcut gets wrong. Worked by hand: 8 / sqrt(95).
    x = [1, 2, 3, 4, 5]
    y = [5, 6, 7, 8, 7]
    import pandas as pd

    ic, _ = attribution._spearman(pd.Series(x), pd.Series(y))
    assert ic == pytest.approx(8 / math.sqrt(95), abs=1e-9)


def test_spearman_is_rank_based_not_linear():
    """A perfect monotone but wildly non-linear relationship must read 1.0."""
    import pandas as pd

    x = pd.Series([1, 2, 3, 4, 5, 6])
    y = pd.Series([1, 10, 1000, 10_000, 1e6, 1e9])
    ic, p = attribution._spearman(x, y)
    assert ic == pytest.approx(1.0)
    assert p == 0.0


def test_constant_input_is_not_a_correlation():
    import pandas as pd

    ic, p = attribution._spearman(pd.Series([1, 1, 1, 1]), pd.Series([1, 2, 3, 4]))
    assert ic is None
    assert p == 1.0


def test_noise_is_reported_as_noise():
    rng = np.random.default_rng(11)
    result = attribution.information_coefficient(
        rng.normal(size=500), rng.normal(size=500), name="noise", horizon_days=21
    )
    assert result is not None
    assert abs(result.ic) < 0.15
    assert result.verdict in ("noise", "weak but real", "inverted - predicts the wrong way")


def test_a_real_relationship_is_found():
    rng = np.random.default_rng(3)
    score = rng.normal(size=600)
    result = attribution.information_coefficient(
        score, 2 * score + rng.normal(scale=3, size=600), name="real", horizon_days=63
    )
    assert result.ic > 0.3
    assert result.p_value < 0.01
    assert result.spread > 0


def test_an_inverted_signal_is_named_as_inverted_not_dismissed():
    """A signal pointing the wrong way is a finding, not a null result."""
    rng = np.random.default_rng(5)
    score = rng.normal(size=600)
    result = attribution.information_coefficient(
        score, -2 * score + rng.normal(scale=3, size=600), name="backwards", horizon_days=63
    )
    assert result.ic < 0
    assert result.verdict.startswith("inverted")


def test_small_samples_refuse_to_render_a_verdict():
    rng = np.random.default_rng(9)
    score = rng.normal(size=20)
    result = attribution.information_coefficient(
        score, score, name="tiny", horizon_days=21
    )
    assert result.verdict == "too few observations"


def test_cross_sectional_ic_ignores_a_market_wide_move():
    """The whole reason this exists.

    Every period here has a different overall level - some months everything
    rose, some months everything fell - but within a period the signal has no
    relationship to the outcome. A pooled correlation can be fooled by that
    common movement; a within-period one must not be.
    """
    rng = np.random.default_rng(17)
    observations = []
    for period in range(40):
        market = rng.normal(scale=8)          # the month's own move
        for _ in range(30):
            score = rng.normal()
            observations.append(
                {"period": period, "sig": score, "return_63": market + rng.normal(scale=2)}
            )

    result = attribution.cross_sectional_ic(observations, "sig", 63)
    assert result is not None
    assert result.periods == 40
    assert abs(result.mean_ic) < 0.1
    assert result.verdict == "noise"


def test_cross_sectional_ic_still_finds_a_within_period_signal():
    rng = np.random.default_rng(19)
    observations = []
    for period in range(40):
        market = rng.normal(scale=8)
        for _ in range(30):
            score = rng.normal()
            observations.append(
                {"period": period, "sig": score,
                 "return_63": market + 3 * score + rng.normal(scale=2)}
            )

    result = attribution.cross_sectional_ic(observations, "sig", 63)
    assert result.mean_ic > 0.5
    assert result.p_value < 0.01
    assert result.share_positive > 0.9


def test_thin_periods_are_dropped_not_averaged_in():
    observations = [{"period": 0, "sig": i, "return_21": i} for i in range(3)]
    assert attribution.cross_sectional_ic(observations, "sig", 21) is None


def test_bonferroni_tightens_as_more_tests_are_run():
    class Fake:
        def __init__(self, p):
            self.p_value = p

    one = [Fake(0.04)]
    assert len(attribution.surviving_correction(one, alpha=0.10)) == 1

    # The same p-value, now one of twenty tests, no longer clears the bar.
    twenty = [Fake(0.04)] + [Fake(0.9)] * 19
    assert len(attribution.surviving_correction(twenty, alpha=0.10)) == 0


def test_forward_return_is_excess_over_the_benchmark():
    import pandas as pd
    from datetime import date

    index = pd.date_range("2024-01-01", periods=10, freq="D")
    stock = pd.DataFrame({"close": [100 * (1.02 ** i) for i in range(10)]}, index=index)
    bench = pd.DataFrame({"close": [100 * (1.01 ** i) for i in range(10)]}, index=index)

    raw = attribution.forward_return(stock, date(2024, 1, 1), 5)
    excess = attribution.forward_return(stock, date(2024, 1, 1), 5, benchmark=bench)

    assert raw == pytest.approx((1.02 ** 5 - 1) * 100, abs=1e-6)
    assert excess == pytest.approx(raw - (1.01 ** 5 - 1) * 100, abs=1e-6)
    assert excess < raw
