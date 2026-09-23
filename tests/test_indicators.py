"""Indicator correctness tests.

RSI is checked two ways against Wilder's worked example: once by deriving the
expected value from the formula inside the test, and once against commonly
published readings with a loose tolerance. Published tables round their
intermediate averages, so the derived check is the strict one. Every dip
signal rests on these numbers, so they are pinned rather than assumed.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src import indicators as ind

# Wilder's worked example. First RSI value appears at index 14.
WILDER_CLOSES = [
    44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08,
    45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64,
    46.21, 46.25, 45.71, 46.45, 45.78, 45.35, 44.03, 44.18, 44.22, 44.57,
    43.42, 42.66, 43.13,
]

# Commonly published RSI(14) readings for the series above, from index 14 on.
# Published tables round their intermediate averages, so these are used as a
# shape-and-level check with a loose tolerance, not as exact truth.
WILDER_RSI_PUBLISHED = [
    70.53, 66.32, 66.55, 69.41, 66.36, 57.97, 62.93, 63.26, 56.06, 62.38,
    54.71, 50.42, 39.99, 41.46, 41.87, 45.46, 37.30, 33.08, 37.77,
]


class TestRSI:
    def test_first_value_matches_the_formula_computed_from_scratch(self):
        """Derive the reference rather than trusting a transcribed table.

        Wilder's seed is the plain average of gains and of losses over the
        first `period` changes. For this series that is 3.34/14 = 0.2385714
        and 1.40/14 = 0.1, giving RS = 2.3857143 and RSI = 70.4641.
        """
        close = pd.Series(WILDER_CLOSES)
        changes = np.diff(np.array(WILDER_CLOSES[:15]))

        avg_gain = changes[changes > 0].sum() / 14
        avg_loss = -changes[changes < 0].sum() / 14
        expected = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)

        assert expected == pytest.approx(70.4641, abs=1e-4)
        assert ind.rsi(close, period=14).iloc[14] == pytest.approx(expected, abs=1e-9)

    def test_tracks_the_published_reference_series(self):
        close = pd.Series(WILDER_CLOSES)
        result = ind.rsi(close, period=14)

        actual = result.iloc[14 : 14 + len(WILDER_RSI_PUBLISHED)].to_numpy()
        expected = np.array(WILDER_RSI_PUBLISHED)

        # Published tables carry rounding drift of a few hundredths; anything
        # larger than 0.1 means the smoothing itself has changed.
        np.testing.assert_allclose(actual, expected, atol=0.1)

    def test_warmup_period_is_nan(self):
        close = pd.Series(WILDER_CLOSES)
        result = ind.rsi(close, period=14)
        assert result.iloc[:14].isna().all(), "RSI must not emit values before a full period"
        assert not np.isnan(result.iloc[14])

    def test_unbroken_gains_reads_100(self):
        close = pd.Series(np.arange(1.0, 40.0))
        result = ind.rsi(close, period=14)
        assert result.iloc[-1] == pytest.approx(100.0)

    def test_flat_series_reads_50(self):
        close = pd.Series([100.0] * 40)
        result = ind.rsi(close, period=14)
        assert result.iloc[-1] == pytest.approx(50.0)

    def test_short_series_returns_all_nan(self):
        result = ind.rsi(pd.Series([10.0, 11.0, 12.0]), period=14)
        assert result.isna().all()


class TestATR:
    def test_true_range_takes_the_largest_of_three(self):
        # Bar 2 gaps down: |low - prev_close| is the widest measure.
        high = pd.Series([10.0, 9.0])
        low = pd.Series([9.0, 7.0])
        close = pd.Series([9.5, 7.5])

        tr = ind.true_range(high, low, close)
        assert tr.iloc[0] == pytest.approx(1.0)      # first bar: H - L
        assert tr.iloc[1] == pytest.approx(2.5)      # |7.0 - 9.5|

    def test_atr_seeds_with_simple_mean_then_smooths(self):
        # Every bar has a true range of exactly 2.0, so ATR must be 2.0.
        n = 30
        high = pd.Series([102.0] * n)
        low = pd.Series([100.0] * n)
        close = pd.Series([101.0] * n)

        result = ind.atr(high, low, close, period=14)
        assert result.iloc[:13].isna().all()
        assert result.iloc[-1] == pytest.approx(2.0)

    def test_atr_pct_is_relative_to_price(self):
        n = 30
        high = pd.Series([102.0] * n)
        low = pd.Series([100.0] * n)
        close = pd.Series([100.0] * n)

        result = ind.atr_pct(high, low, close, period=14)
        assert result.iloc[-1] == pytest.approx(2.0)  # ATR 2.0 on a price of 100


class TestDrawdown:
    def test_measures_distance_below_the_rolling_high(self):
        close = pd.Series([100.0, 120.0, 110.0, 90.0])
        result = ind.drawdown_from_high_pct(close)

        assert result.iloc[0] == pytest.approx(0.0)
        assert result.iloc[1] == pytest.approx(0.0)
        assert result.iloc[2] == pytest.approx(pytest.approx(8.3333, abs=1e-3))
        assert result.iloc[3] == pytest.approx(25.0)   # 90 vs a high of 120

    def test_at_the_high_is_zero(self):
        close = pd.Series(np.arange(1.0, 100.0))
        assert ind.drawdown_from_high_pct(close).iloc[-1] == pytest.approx(0.0)


class TestTrendFilter:
    def test_slope_is_positive_in_an_uptrend(self):
        close = pd.Series(np.linspace(100, 200, 400))
        dma = ind.sma(close, 200)
        slope = ind.dma_slope_pct(dma, lookback=126)
        assert slope.iloc[-1] > 0

    def test_slope_is_negative_in_a_downtrend(self):
        close = pd.Series(np.linspace(200, 100, 400))
        dma = ind.sma(close, 200)
        slope = ind.dma_slope_pct(dma, lookback=126)
        assert slope.iloc[-1] < 0

    def test_sma_requires_a_full_window(self):
        close = pd.Series(np.arange(1.0, 10.0))
        result = ind.sma(close, 200)
        assert result.isna().all(), "SMA must not produce partial-window averages"


class TestSupport:
    def test_finds_a_local_minimum(self):
        # V shape: the trough at index 10 is the only genuine pivot.
        values = list(np.arange(20.0, 9.0, -1.0)) + list(np.arange(11.0, 21.0))
        low = pd.Series(values)
        mask = ind.swing_lows(low, order=5)
        assert mask.sum() == 1
        assert low[mask].iloc[0] == pytest.approx(10.0)

    def test_nearest_support_sits_at_or_below_price(self):
        values = list(np.arange(20.0, 9.0, -1.0)) + list(np.arange(11.0, 21.0))
        low = pd.Series(values)
        support = ind.nearest_support(15.0, low, order=5)
        assert support == pytest.approx(10.0)

    def test_no_support_below_price_returns_none(self):
        values = list(np.arange(20.0, 9.0, -1.0)) + list(np.arange(11.0, 21.0))
        low = pd.Series(values)
        assert ind.nearest_support(5.0, low, order=5) is None


class TestDelivery:
    def test_delivery_pct_is_a_share_of_traded_volume(self):
        result = ind.delivery_pct(pd.Series([400.0]), pd.Series([1000.0]))
        assert result.iloc[0] == pytest.approx(40.0)

    def test_zero_volume_does_not_divide_by_zero(self):
        result = ind.delivery_pct(pd.Series([0.0]), pd.Series([0.0]))
        assert result.isna().all()

    def test_ratio_is_reported_only_on_down_days(self):
        close = pd.Series([100.0] * 10 + [99.0, 100.5])
        delivery = pd.Series([40.0] * 12)

        result = ind.down_day_delivery_ratio(close, delivery, window=5)
        assert not np.isnan(result.iloc[10]), "down day must produce a reading"
        assert np.isnan(result.iloc[11]), "up day must not produce a reading"

    def test_elevated_delivery_on_a_decline_reads_above_one(self):
        close = pd.Series([100.0] * 10 + [98.0])
        delivery = pd.Series([40.0] * 10 + [60.0])

        result = ind.down_day_delivery_ratio(close, delivery, window=10)
        assert result.iloc[-1] > 1.0

    def test_persistence_rejects_a_single_block_deal_spike(self):
        # One 3x delivery day, the rest ordinary: not accumulation.
        close = pd.Series([100.0 - i for i in range(20)])
        delivery = pd.Series([30.0] * 19 + [90.0])

        count = ind.delivery_persistence(close, delivery, window=10, lookback=10, ratio_threshold=1.10)
        assert count == 1, "a lone spike must not clear a 3-session requirement"

    def test_persistence_counts_sustained_elevation(self):
        close = pd.Series([100.0 - i for i in range(20)])
        delivery = pd.Series([30.0] * 15 + [60.0] * 5)

        count = ind.delivery_persistence(close, delivery, window=10, lookback=5, ratio_threshold=1.10)
        assert count >= 3


class TestIndicatorFrame:
    def _frame(self, n: int = 400) -> pd.DataFrame:
        rng = np.random.default_rng(42)
        close = pd.Series(100 + np.cumsum(rng.normal(0.05, 1.0, n)))
        return pd.DataFrame(
            {
                "open": close.shift(1).fillna(close.iloc[0]),
                "high": close * 1.01,
                "low": close * 0.99,
                "close": close,
                "volume": rng.integers(100_000, 500_000, n).astype(float),
                "deliverable_qty": rng.integers(40_000, 200_000, n).astype(float),
            }
        )

    def test_attaches_every_column_the_screener_needs(self):
        result = ind.compute_indicator_frame(self._frame())
        for column in [
            "rsi", "atr", "atr_pct", "dma_200", "dma_50", "dma_long_slope_pct",
            "high_52w", "low_52w", "drawdown_pct", "delivery_pct",
            "delivery_avg", "down_day_delivery_ratio",
        ]:
            assert column in result.columns, f"missing {column}"

    def test_does_not_mutate_the_input(self):
        df = self._frame()
        before = list(df.columns)
        ind.compute_indicator_frame(df)
        assert list(df.columns) == before

    def test_rejects_a_frame_without_ohlc(self):
        with pytest.raises(ValueError, match="missing required columns"):
            ind.compute_indicator_frame(pd.DataFrame({"close": [1.0, 2.0]}))

    def test_works_without_delivery_data(self):
        df = self._frame().drop(columns=["deliverable_qty"])
        result = ind.compute_indicator_frame(df)
        assert "rsi" in result.columns
        assert "delivery_pct" not in result.columns
