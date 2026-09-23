"""Market regime: is the broad market itself in an uptrend or a downtrend?

The per-stock 200-DMA gate in `screener.evaluate_dip` asks whether *this
stock's* trend is intact. It says nothing about the market around it, and the
measurement on 2026-09-23 showed that matters: the candidate ranking carried
real information when the market went on to rise and none when it fell.

That split used the market's *future* return, which cannot be known at entry.
This module answers the tradeable version of the question - what does the
market look like *today* - using only index bars up to the date in question.

The definition is deliberately the textbook one, fixed before any results
were looked at, because the history holds only a handful of independent bear
phases and any threshold tuned against them would be fitted to those few
episodes rather than to markets in general:

  UPTREND    index above its 200-DMA and the 200-DMA rising
  DOWNTREND  index below its 200-DMA and the 200-DMA falling
  MIXED      the two disagree - a trend turning, in either direction
  UNKNOWN    not enough history to say. Never treated as either trend.

`regime_frame` computes the whole series once for the backtest harness;
`classify` reads one date from it for production. Both run the same code, so
the regime the dashboard shows is the regime that was measured.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

import logging

import pandas as pd

log = logging.getLogger(__name__)

UPTREND = "UPTREND"
MIXED = "MIXED"
DOWNTREND = "DOWNTREND"
UNKNOWN = "UNKNOWN"

LABELS = (UPTREND, MIXED, DOWNTREND)


@dataclass(frozen=True)
class MarketRegime:
    label: str
    as_of: date | None
    index_close: float | None = None
    index_dma: float | None = None
    pct_vs_dma: float | None = None
    dma_slope_pct: float | None = None
    drawdown_pct: float | None = None
    reason: str = ""

    @property
    def known(self) -> bool:
        return self.label != UNKNOWN

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["as_of"] = self.as_of.isoformat() if self.as_of else None
        return out


def regime_frame(
    index: pd.DataFrame,
    *,
    dma: int = 200,
    slope_lookback: int = 126,
    high_lookback: int = 252,
) -> pd.DataFrame:
    """Regime for every date in the index history, point-in-time.

    Each row uses only bars up to and including its own date: rolling windows
    look backwards, and the slope compares today's average with the average
    `slope_lookback` sessions ago.
    """
    if index is None or index.empty or "close" not in index.columns:
        return pd.DataFrame(columns=["label"])

    close = index["close"].astype(float)
    average = close.rolling(dma, min_periods=dma).mean()
    earlier = average.shift(slope_lookback)
    high = close.rolling(high_lookback, min_periods=high_lookback // 2).max()

    frame = pd.DataFrame(
        {
            "close": close,
            "dma": average,
            "pct_vs_dma": (close - average) / average * 100.0,
            "dma_slope_pct": (average - earlier) / earlier * 100.0,
            "drawdown_pct": (high - close) / high * 100.0,
        },
        index=index.index,
    )

    above = frame["pct_vs_dma"] > 0
    rising = frame["dma_slope_pct"] > 0
    known = frame["pct_vs_dma"].notna() & frame["dma_slope_pct"].notna()

    label = pd.Series(MIXED, index=frame.index, dtype=object)
    label[above & rising] = UPTREND
    label[~above & ~rising] = DOWNTREND
    label[~known] = UNKNOWN
    frame["label"] = label
    return frame


def classify(
    index: pd.DataFrame | None,
    as_of: date | None = None,
    *,
    dma: int = 200,
    slope_lookback: int = 126,
    frame: pd.DataFrame | None = None,
) -> MarketRegime:
    """The regime on `as_of` (default: the latest bar).

    Pass a precomputed `frame` from `regime_frame` when classifying many
    dates, to avoid recomputing the rolling windows each time.
    """
    if frame is None:
        if index is None or index.empty:
            return MarketRegime(UNKNOWN, as_of, reason="no index history available")
        frame = regime_frame(index, dma=dma, slope_lookback=slope_lookback)

    if frame.empty:
        return MarketRegime(UNKNOWN, as_of, reason="no index history available")

    rows = frame if as_of is None else frame[frame.index <= pd.Timestamp(as_of)]
    if rows.empty:
        return MarketRegime(UNKNOWN, as_of, reason="no index bars on or before this date")

    stamp = rows.index[-1]
    row = rows.iloc[-1]
    when = stamp.date()

    def _num(key: str) -> float | None:
        value = row.get(key)
        return None if value is None or pd.isna(value) else round(float(value), 2)

    label = row["label"]
    if label == UNKNOWN:
        return MarketRegime(
            UNKNOWN, when, reason=f"need {dma + slope_lookback} sessions of index history"
        )

    pct, slope = _num("pct_vs_dma"), _num("dma_slope_pct")
    side = "above" if pct is not None and pct > 0 else "below"
    direction = "rising" if slope is not None and slope > 0 else "falling"
    reason = (
        f"Nifty 500 is {abs(pct or 0):.1f}% {side} its {dma}-DMA, "
        f"and that average is {direction} ({slope:+.1f}% over {slope_lookback} sessions)."
    )

    return MarketRegime(
        label=label,
        as_of=when,
        index_close=_num("close"),
        index_dma=_num("dma"),
        pct_vs_dma=pct,
        dma_slope_pct=slope,
        drawdown_pct=_num("drawdown_pct"),
        reason=reason,
    )


def episodes(labels: pd.Series) -> dict[str, int]:
    """How many separate runs of each regime a series contains.

    The number that matters for how much to trust any regime result: a
    thousand observations drawn from two bear markets are two data points
    about bear markets, not a thousand.
    """
    labels = labels[labels != UNKNOWN]
    if labels.empty:
        return {}
    runs = labels.ne(labels.shift()).cumsum()
    first = labels.groupby(runs).first()
    return first.value_counts().to_dict()


# --- Production ---------------------------------------------------------------

INFORM = "inform"
REDUCE = "reduce"
BLOCK = "block"
ACTIONS = (INFORM, REDUCE, BLOCK)


def current(cfg: Any) -> MarketRegime:
    """Today's regime, from the configured index.

    Any failure to fetch yields UNKNOWN rather than an exception: the regime
    informs a decision, it is not a precondition for one, and "we could not
    look" must never be read as either trend.
    """
    from src.data import prices

    dma = int(cfg.get("regime.dma", 200))
    lookback = int(cfg.get("regime.slope_lookback_days", 126))
    try:
        result = prices.get_index_history(cfg.get("regime.index", "nifty500"), years=3)
    except Exception as exc:  # network, parsing - all mean the same thing here
        return MarketRegime(UNKNOWN, None, reason=f"index history unavailable: {exc}")

    if not result.usable or result.value is None or result.value.empty:
        return MarketRegime(UNKNOWN, None, reason="index history unavailable")
    return classify(result.value, dma=dma, slope_lookback=lookback)


def action(cfg: Any) -> str:
    """The configured downtrend policy, defaulting to the measured one."""
    value = str(cfg.get("regime.downtrend_action", INFORM) or INFORM).strip().lower()
    if value not in ACTIONS:
        log.warning("regime.downtrend_action=%r is not one of %s; using 'inform'", value, ACTIONS)
        return INFORM
    return value


def apply_policy(decision: Any, market: MarketRegime | None, cfg: Any) -> Any:
    """Adjust a sizing decision for the market regime, per the configured policy.

    Only a *known* DOWNTREND triggers anything. UNKNOWN is never treated as a
    downtrend - an outage at the index provider must not quietly halve or
    cancel a position - and MIXED is a turn in either direction, which the
    measurement gave no reason to act on.
    """
    if market is None or market.label != DOWNTREND or not decision.is_buy:
        return decision

    policy = action(cfg)
    if policy == INFORM:
        decision.adjustments.append(
            "Market regime is DOWNTREND. Policy is 'inform': size unchanged, because dip "
            "entries made in downtrends measured as the best in the 2015-26 sample."
        )
        return decision

    if policy == BLOCK:
        decision.recommendation = "NO_BUY"
        decision.rejections.append(
            "market regime is DOWNTREND and regime.downtrend_action is 'block'"
        )
        decision.total_value = 0.0
        decision.total_shares = 0
        decision.pct_of_capital = 0.0
        decision.risk_amount = 0.0
        decision.risk_pct_of_capital = 0.0
        decision.tranches = []
        decision.narrative = (
            "NO BUY while the market is in a downtrend (policy: block). Note this rule was "
            "measured to remove the strongest entries in the backtest, not the weakest."
        )
        return decision

    # REDUCE
    factor = float(cfg.get("regime.reduce_factor", 0.5))
    factor = max(0.0, min(1.0, factor))
    per_share = decision.total_value / decision.total_shares if decision.total_shares else 0.0

    shares = int(decision.total_shares * factor)
    if shares <= 0 or per_share <= 0:
        decision.recommendation = "NO_BUY"
        decision.rejections.append(
            f"downtrend reduction to {factor:.0%} leaves less than one share"
        )
        decision.total_value = 0.0
        decision.total_shares = 0
        decision.tranches = []
        return decision

    scale = shares / decision.total_shares
    decision.total_shares = shares
    decision.total_value = round(shares * per_share, 2)
    decision.pct_of_capital *= scale
    decision.risk_amount *= scale
    decision.risk_pct_of_capital *= scale
    for tranche in decision.tranches:
        tranche.shares = int(tranche.shares * factor)
        tranche.value = round(tranche.value * factor, 2)
    decision.adjustments.append(
        f"Market regime is DOWNTREND: position scaled to {factor:.0%} (policy: reduce)."
    )
    decision.narrative = (decision.narrative + " " if decision.narrative else "") + (
        f"Scaled to {factor:.0%} of the computed size because the market is in a downtrend."
    )
    return decision


def describe_for_humans(market: MarketRegime | None) -> str:
    """One sentence for summaries and alerts, with what the regime implies."""
    if market is None or not market.known:
        reason = market.reason if market else "not checked"
        return f"Market regime unknown ({reason})."
    meaning = {
        UPTREND: "The candidate ranking has been most informative in this regime.",
        MIXED: "The trend is turning; neither regime's history applies cleanly.",
        DOWNTREND: (
            "In 2015-26, dips bought in a downtrend did better than any other regime, "
            "but that rests on three episodes and one of them lost money. The candidate "
            "ranking told you nothing in this regime, so spread across candidates "
            "rather than trusting the top-ranked one."
        ),
    }[market.label]
    return f"Market regime: {market.label}. {market.reason} {meaning}"


def downtrend_transition(cfg: Any) -> tuple[MarketRegime, MarketRegime] | None:
    """(yesterday, today) if the market just entered or left DOWNTREND.

    Stateless: both regimes come from the same index history, so there is
    nothing to store and nothing to drift. Only moves into or out of
    DOWNTREND count - there were seven in twelve years. MIXED and UPTREND
    swap several times a year, and alerting on those would be noise.
    """
    from src.data import prices

    try:
        result = prices.get_index_history(cfg.get("regime.index", "nifty500"), years=3)
    except Exception:
        return None
    if not result.usable or result.value is None or len(result.value) < 2:
        return None

    frame = regime_frame(
        result.value,
        dma=int(cfg.get("regime.dma", 200)),
        slope_lookback=int(cfg.get("regime.slope_lookback_days", 126)),
    )
    today = classify(None, frame=frame)
    yesterday = classify(None, frame.index[-2].date(), frame=frame)

    if not (today.known and yesterday.known) or today.label == yesterday.label:
        return None
    if DOWNTREND not in (today.label, yesterday.label):
        return None
    return yesterday, today
