"""Does a signal predict returns, or does it only look like it does?

The information coefficient is the Spearman rank correlation between a score
made *before* an outcome and the outcome itself. Rank rather than linear,
because we care whether higher scores finish higher, not whether the
relationship is a straight line.

Reading an IC honestly matters more than computing one:

  |IC| < 0.03   indistinguishable from noise at any realistic sample size
  0.03 - 0.05   weak, and worth something only across many positions
  0.05 - 0.10   genuinely useful in quantitative equity
  > 0.15        suspect a look-ahead bug before celebrating

A p-value is reported alongside because an IC of 0.20 on 25 observations
means nothing. And a negative IC is a finding, not a failure - a signal that
reliably points the wrong way can be inverted, whereas one sitting at zero
is simply dead weight.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Sequence

import math

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

#: Below this, a correlation is not distinguishable from noise.
NOISE_FLOOR = 0.03


@dataclass
class ICResult:
    """One signal measured against one horizon."""

    name: str
    horizon_days: int
    ic: float
    p_value: float
    n: int
    hit_rate: float          # share of the time the sign was right
    mean_top_quintile: float
    mean_bottom_quintile: float

    @property
    def verdict(self) -> str:
        if self.n < 30:
            return "too few observations"
        if self.p_value > 0.10:
            return "noise"
        if abs(self.ic) < NOISE_FLOOR:
            return "noise"
        if self.ic < 0:
            return "inverted - predicts the wrong way"
        if self.ic < 0.05:
            return "weak but real"
        if self.ic < 0.15:
            return "useful"
        return "strong - check for look-ahead"

    @property
    def spread(self) -> float:
        """Top quintile minus bottom. What the signal is worth in practice."""
        return self.mean_top_quintile - self.mean_bottom_quintile

    def describe(self) -> str:
        return (
            f"{self.name:<28} {self.horizon_days:>4}d  IC {self.ic:+.3f}  "
            f"p {self.p_value:.3f}  n {self.n:<5} "
            f"spread {self.spread:+6.1f}%  {self.verdict}"
        )


def information_coefficient(
    scores: Sequence[float],
    forward_returns: Sequence[float],
    *,
    name: str = "signal",
    horizon_days: int = 0,
) -> ICResult | None:
    """Spearman rank correlation between a score and what happened next."""
    frame = pd.DataFrame({"score": scores, "ret": forward_returns}).dropna()
    if len(frame) < 10 or frame["score"].nunique() < 3:
        return None

    ic, p_value = _spearman(frame["score"], frame["ret"])
    if ic is None:
        return None

    # Hit rate against the median, not zero: in a rising market almost
    # everything is positive, and a signal that "predicts" that is predicting
    # the market, not the stock.
    median_score = frame["score"].median()
    median_ret = frame["ret"].median()
    above = frame["score"] > median_score
    hits = ((above) & (frame["ret"] > median_ret)) | ((~above) & (frame["ret"] <= median_ret))

    quintile = max(1, len(frame) // 5)
    ranked = frame.sort_values("score")

    return ICResult(
        name=name,
        horizon_days=horizon_days,
        ic=float(ic),
        p_value=float(p_value),
        n=len(frame),
        hit_rate=float(hits.mean()),
        mean_top_quintile=float(ranked["ret"].tail(quintile).mean()),
        mean_bottom_quintile=float(ranked["ret"].head(quintile).mean()),
    )



def _spearman(x: pd.Series, y: pd.Series) -> tuple[float | None, float]:
    """Spearman correlation and a two-sided p-value, without scipy.

    Spearman is Pearson on ranks, and pandas ranks handle ties correctly.
    The p-value comes from the usual t approximation,
    t = r * sqrt((n - 2) / (1 - r^2)), converted through an erf-based normal
    CDF. That approximation is good from roughly n = 30 upward, which is
    where an IC becomes worth reading at all - and `verdict` already refuses
    to interpret anything smaller.

    scipy is avoided deliberately: its compiled extensions are blocked by
    Application Control on the development machine, the same policy that
    ruled out psycopg2.
    """
    n = len(x)
    if n < 3:
        return None, 1.0

    ranked_x = x.rank()
    ranked_y = y.rank()
    if ranked_x.std() == 0 or ranked_y.std() == 0:
        return None, 1.0

    r = float(np.corrcoef(ranked_x, ranked_y)[0, 1])
    if math.isnan(r):
        return None, 1.0

    if abs(r) >= 1.0 or n <= 2:
        return r, 0.0

    t = r * math.sqrt((n - 2) / (1 - r * r))
    # Two-sided, via the normal CDF: erfc(|t| / sqrt(2)).
    p = math.erfc(abs(t) / math.sqrt(2.0))
    return r, min(1.0, p)

def forward_return(
    frame: pd.DataFrame,
    entry_date: date,
    horizon_days: int,
    *,
    benchmark: pd.DataFrame | None = None,
) -> float | None:
    """Return over `horizon_days` sessions after `entry_date`.

    Excess over the benchmark when one is supplied. That matters: in a market
    that rose 20% over the period, a signal picking stocks that rose 20% has
    predicted nothing about the stocks.
    """
    if frame is None or frame.empty or "close" not in frame.columns:
        return None

    stamp = pd.Timestamp(entry_date)
    after = frame.index[frame.index >= stamp]
    if len(after) < horizon_days + 1:
        return None

    start = float(frame.loc[after[0], "close"])
    end = float(frame.loc[after[horizon_days], "close"])
    if start <= 0:
        return None
    result = (end - start) / start * 100.0

    if benchmark is not None and not benchmark.empty:
        bench_after = benchmark.index[benchmark.index >= stamp]
        if len(bench_after) >= horizon_days + 1:
            b_start = float(benchmark.loc[bench_after[0], "close"])
            b_end = float(benchmark.loc[bench_after[horizon_days], "close"])
            if b_start > 0:
                result -= (b_end - b_start) / b_start * 100.0

    return result


def study(
    observations: list[dict[str, Any]],
    signal_names: Sequence[str],
    horizons: Sequence[int] = (21, 63, 126),
) -> list[ICResult]:
    """Measure every signal against every horizon.

    Each observation is a dict of signal values plus `return_<horizon>` keys.
    """
    results: list[ICResult] = []
    for horizon in horizons:
        key = f"return_{horizon}"
        returns = [o.get(key) for o in observations]
        for name in signal_names:
            result = information_coefficient(
                [o.get(name) for o in observations], returns,
                name=name, horizon_days=horizon,
            )
            if result:
                results.append(result)
    return results


def report(results: list[ICResult]) -> str:
    """A readable table, strongest first, with the caveats attached."""
    if not results:
        return "No signal had enough observations to measure."

    lines = [
        f"{'signal':<28} {'horiz':>5}  {'IC':>7}  {'p':>6}  {'n':<6} {'spread':>8}  verdict",
        "-" * 96,
    ]
    for result in sorted(results, key=lambda r: -abs(r.ic)):
        lines.append("  " + result.describe())

    real = [r for r in results if r.verdict in ("weak but real", "useful", "strong - check for look-ahead")]
    inverted = [r for r in results if r.verdict.startswith("inverted")]

    lines.append("")
    lines.append(f"{len(real)} of {len(results)} measurements show a real relationship.")
    if inverted:
        lines.append(
            f"{len(inverted)} point the wrong way - those signals are worse than useless "
            f"as currently scored, and inverting them would help."
        )
    return "\n".join(lines)


@dataclass
class CrossSectionalIC:
    """A signal measured within each period, then averaged across periods.

    The pooled IC in `information_coefficient` has a known weakness: it mixes
    two different questions. If every stock sampled in March did well and March
    also happened to have high readings on some signal, the pooled correlation
    records that as predictive power when it is really just a fact about March.

    Measuring inside each period and averaging removes the market entirely -
    every observation in a period faced the same market - so what remains is
    only whether the signal ranked stocks *against each other*. That is the
    question a screener actually poses. The t-statistic is computed on the
    series of per-period ICs, which also gives an honest standard error:
    n is the number of independent periods, not the number of stock-days.
    """

    name: str
    horizon_days: int
    mean_ic: float
    t_stat: float
    p_value: float
    periods: int
    share_positive: float

    @property
    def verdict(self) -> str:
        if self.periods < 12:
            return "too few periods"
        if self.p_value > 0.10:
            return "noise"
        if abs(self.mean_ic) < NOISE_FLOOR:
            return "noise"
        if self.mean_ic < 0:
            return "inverted - predicts the wrong way"
        if self.mean_ic < 0.05:
            return "weak but real"
        return "useful"

    def describe(self) -> str:
        return (
            f"{self.name:<28} {self.horizon_days:>4}d  IC {self.mean_ic:+.3f}  "
            f"t {self.t_stat:+5.2f}  p {self.p_value:.3f}  "
            f"{self.periods:>3} periods  {self.share_positive:4.0%} positive  {self.verdict}"
        )


def cross_sectional_ic(
    observations: list[dict[str, Any]],
    signal: str,
    horizon_days: int,
    *,
    period_key: str = "period",
    min_per_period: int = 15,
) -> CrossSectionalIC | None:
    """Rank stocks against each other within a period, then average."""
    return_key = f"return_{horizon_days}"

    buckets: dict[Any, list[tuple[float, float]]] = {}
    for obs in observations:
        score, ret = obs.get(signal), obs.get(return_key)
        if score is None or ret is None or obs.get(period_key) is None:
            continue
        buckets.setdefault(obs[period_key], []).append((float(score), float(ret)))

    per_period: list[float] = []
    for pairs in buckets.values():
        if len(pairs) < min_per_period:
            continue
        xs = pd.Series([p[0] for p in pairs])
        ys = pd.Series([p[1] for p in pairs])
        ic, _ = _spearman(xs, ys)
        if ic is not None:
            per_period.append(ic)

    if len(per_period) < 5:
        return None

    series = np.array(per_period)
    mean = float(series.mean())
    sd = float(series.std(ddof=1))
    n = len(series)

    if sd == 0:
        t_stat, p_value = 0.0, 1.0
    else:
        t_stat = mean / (sd / math.sqrt(n))
        p_value = math.erfc(abs(t_stat) / math.sqrt(2.0))

    return CrossSectionalIC(
        name=signal,
        horizon_days=horizon_days,
        mean_ic=mean,
        t_stat=float(t_stat),
        p_value=float(min(1.0, p_value)),
        periods=n,
        share_positive=float((series > 0).mean()),
    )


def surviving_correction(results: Sequence[Any], alpha: float = 0.10) -> list[Any]:
    """Which results survive the fact that many tests were run at once.

    Testing seven signals against three horizons is twenty-one chances for
    something to look significant by luck. At a 0.10 cutoff roughly two will,
    every time, on data with no signal in it whatsoever. Bonferroni is the
    blunt correction - divide the threshold by the number of tests - and it is
    deliberately conservative, which is the right direction to err when the
    output decides how much money to put on a position.
    """
    if not results:
        return []
    threshold = alpha / len(results)
    return [r for r in results if r.p_value <= threshold]
