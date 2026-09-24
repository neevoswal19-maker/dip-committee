"""The trade simulator: fills, stops, targets, costs, scale-ins, trims.

Every research number rests on this, so these tests pin exact prices and
exact bars. A simulator that fills a stop one bar late or forgets a charge
produces a report that is confidently wrong.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from src.config import load_config
from src.strategy import swing
from src.strategy.exit_policies import LongTermPlan, LongTermPolicy, SwingPolicy
from src.strategy.simulator import (
    NEXT_OPEN, SAME_CLOSE, Bars, curve_stats, simulate, trade_stats,
)

CFG = load_config()


def bars(symbol, opens, highs, lows, closes, *, atr=2.0, atr22=None):
    index = pd.bdate_range("2024-01-01", periods=len(closes))
    frame = pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes,
                          "atr": atr}, index=index)
    extra = ("atr",)
    if atr22 is not None:
        frame["atr22"] = atr22
        extra = ("atr", "atr22")
    return Bars.from_frame(symbol, frame, extra=extra)


def flat(n=12, price=100.0):
    p = [price] * n
    return p, p, p, p


def signal_at(n, *positions):
    s = np.zeros(n, dtype=bool)
    for p in positions:
        s[p] = True
    return s


def run(b: dict, signals: dict, plan, *, mode=NEXT_OPEN, rule=None, costs=False,
        max_positions=None, ranks=None, position_value=10_000.0, exits=None):
    rule = rule or swing.RULES["rsi2"]
    ranks = ranks or {s: np.zeros(len(x.dates)) for s, x in b.items()}
    exits = exits or {s: np.zeros(len(x.dates), dtype=bool) for s, x in b.items()}
    first = min(x.dates[0] for x in b.values())
    last = max(x.dates[-1] for x in b.values())
    return simulate(
        b, signals, ranks, lambda s: SwingPolicy(rule, plan, exits[s]),
        strategy=rule.key, start=first, end=last, mode=mode,
        position_value=position_value, max_positions=max_positions,
        first_tranche=rule.scale_in[0], cfg=CFG, with_costs=costs,
    )


def only_trade(result):
    assert len(result.trades) == 1, [t.exit_reason for t in result.trades]
    return result.trades[0]


# --- Fills ------------------------------------------------------------------------


def test_next_open_mode_fills_at_the_next_bars_open():
    n = 10
    opens = [100 + i for i in range(n)]
    closes = [100.5 + i for i in range(n)]
    b = {"A": bars("A", opens, [c + 1 for c in closes], [o - 1 for o in opens], closes)}
    trade = only_trade(run(b, {"A": signal_at(n, 2)}, swing.ExitPlan(None, None, 3)))

    entry = trade.fills[0]
    assert entry.when == b["A"].dates[3] and entry.price == 103
    # Time stop: sessions 3, 4, 5 -> decided at bar 5's close, filled at bar 6's open.
    assert trade.exit_reason == "time_stop"
    assert trade.fills[-1].when == b["A"].dates[6] and trade.fills[-1].price == 106


def test_same_close_mode_fills_at_the_signal_bars_close():
    n = 10
    opens = [100 + i for i in range(n)]
    closes = [100.5 + i for i in range(n)]
    b = {"A": bars("A", opens, [c + 1 for c in closes], [o - 1 for o in opens], closes)}
    trade = only_trade(run(b, {"A": signal_at(n, 2)}, swing.ExitPlan(None, None, 3), mode=SAME_CLOSE))
    assert trade.fills[0].when == b["A"].dates[2] and trade.fills[0].price == 102.5
    assert trade.fills[-1].when == b["A"].dates[5] and trade.fills[-1].price == 105.5


def test_a_rule_exit_fills_at_the_next_open():
    o, h, l, c = flat()
    o = list(o); o[6] = 104.0
    b = {"A": bars("A", o, [max(x, 104.0) for x in h], l, c)}
    exits = {"A": signal_at(12, 5)}
    trade = only_trade(run(b, {"A": signal_at(12, 1)}, swing.ExitPlan(None, None, 10), exits=exits))
    assert trade.exit_reason == "rule_exit"
    assert trade.fills[-1].when == b["A"].dates[6] and trade.fills[-1].price == 104.0


# --- Stops and targets --------------------------------------------------------------


def test_stop_fills_at_the_stop():
    o, h, l, c = (list(x) for x in flat())
    l[4] = 95.0; o[4] = 99.0
    b = {"A": bars("A", o, h, l, c, atr=2.0)}
    trade = only_trade(run(b, {"A": signal_at(12, 1)}, swing.ExitPlan(2.0, None, 10)))
    assert trade.stop == 96.0
    assert trade.exit_reason == "stop"
    assert trade.fills[-1].when == b["A"].dates[4] and trade.fills[-1].price == 96.0


def test_a_gap_through_the_stop_fills_at_the_open():
    o, h, l, c = (list(x) for x in flat())
    o[4], l[4], c[4], h[4] = 94.0, 93.0, 94.0, 94.5
    b = {"A": bars("A", o, h, l, c, atr=2.0)}
    trade = only_trade(run(b, {"A": signal_at(12, 1)}, swing.ExitPlan(2.0, None, 10)))
    assert trade.fills[-1].price == 94.0


def test_target_fills_at_the_target_and_a_gap_fills_at_the_open():
    o, h, l, c = (list(x) for x in flat())
    h[3] = 103.0
    b = {"A": bars("A", o, h, l, c, atr=2.0)}
    trade = only_trade(run(b, {"A": signal_at(12, 1)}, swing.ExitPlan(None, 1.0, 10)))
    assert trade.target == 102.0
    assert (trade.exit_reason, trade.fills[-1].price) == ("target", 102.0)

    o2, h2, l2, c2 = (list(x) for x in flat())
    o2[3], h2[3], c2[3] = 104.0, 105.0, 104.5
    b2 = {"A": bars("A", o2, h2, l2, c2, atr=2.0)}
    trade2 = only_trade(run(b2, {"A": signal_at(12, 1)}, swing.ExitPlan(None, 1.0, 10)))
    assert trade2.fills[-1].price == 104.0


def test_when_one_bar_touches_both_the_stop_is_assumed_first():
    o, h, l, c = (list(x) for x in flat())
    h[3], l[3] = 105.0, 95.0
    b = {"A": bars("A", o, h, l, c, atr=2.0)}
    trade = only_trade(run(b, {"A": signal_at(12, 1)}, swing.ExitPlan(2.0, 1.0, 10)))
    assert trade.exit_reason == "stop"


# --- Costs -----------------------------------------------------------------------------


def test_a_flat_trade_loses_money_after_costs():
    b = {"A": bars("A", *flat())}
    result = run(b, {"A": signal_at(12, 1)}, swing.ExitPlan(None, None, 3), costs=True,
                 position_value=50_000.0)
    trade = only_trade(result)
    buy, sell = trade.fills[0], trade.fills[-1]
    assert buy.charges > 0 and sell.charges > 0
    assert sell.charges > 10            # the DP charge alone is ~Rs 16
    assert trade.pnl < 0 and not trade.is_win
    # Round trip at Rs 50k is roughly a third of a percent including slippage.
    assert -0.6 < trade.return_pct < -0.25
    assert trade_stats(result.trades)["win_rate"] == 0.0


# --- Portfolio ---------------------------------------------------------------------------


def test_the_portfolio_never_exceeds_its_slots_and_takes_the_lowest_rank():
    b = {s: bars(s, *flat()) for s in ("A", "B", "C")}
    signals = {s: signal_at(12, 1) for s in b}
    ranks = {"A": np.full(12, 3.0), "B": np.full(12, 1.0), "C": np.full(12, 2.0)}
    result = run(b, signals, swing.ExitPlan(None, None, 5), max_positions=2, ranks=ranks,
                 position_value=1_000.0)
    assert {t.symbol for t in result.trades} == {"B", "C"}


# --- Scale-in ------------------------------------------------------------------------------


def test_tps_adds_tranches_on_each_lower_close():
    n = 12
    closes = [100.0 - i for i in range(n)]
    opens = [c + 0.2 for c in closes]
    b = {"A": bars("A", opens, [o + 0.5 for o in opens], [c - 0.5 for c in closes], closes)}
    rule = swing.RULES["tps"]
    trade = only_trade(run(b, {"A": signal_at(n, 1)}, swing.ExitPlan(None, None, 20),
                           rule=rule, position_value=10_000.0))
    buys = [f for f in trade.fills if f.side == "BUY"]
    assert [f.quantity for f in buys] == [
        int(1000 // opens[2]), int(2000 // opens[3]), int(3000 // opens[4]), int(4000 // opens[5])
    ]
    assert trade.tranches_done == 4
    expected_avg = sum(f.quantity * f.price for f in buys) / sum(f.quantity for f in buys)
    assert trade.avg_entry == pytest.approx(expected_avg)


# --- Long-term plans --------------------------------------------------------------------------


def _long_term(b, plan, signal):
    first = b["A"].dates[0]
    last = b["A"].dates[-1]
    return simulate(b, {"A": signal}, {"A": np.zeros(len(b["A"].dates))},
                    lambda s: LongTermPolicy(plan), strategy="lt", start=first, end=last,
                    position_value=10_000.0, cfg=CFG, with_costs=False)


def test_staged_trims_sell_a_quarter_at_25_and_at_50_percent():
    n = 40
    closes = [100 * 1.02 ** i for i in range(n)]
    opens = [c / 1.005 for c in closes]
    b = {"A": bars("A", opens, [c * 1.01 for c in closes], [o * 0.99 for o in opens], closes)}
    plan = LongTermPlan("T", "trims", trims=((25.0, 0.25), (50.0, 0.25)))
    trade = _long_term(b, plan, signal_at(n, 1)).trades[0]

    entry = trade.first_price
    original = trade.fills[0].quantity
    trims = [f for f in trade.fills if f.reason.startswith("trim")]
    assert [f.reason for f in trims] == ["trim_25pct", "trim_50pct"]
    assert trims[0].quantity == int(original * 0.25)
    assert trims[0].price >= entry * 1.25 - 1e-6
    assert trims[1].price >= entry * 1.50 - 1e-6


def test_chandelier_stop_trails_the_high():
    rising = [100.0 + i for i in range(20)]
    falling = [118.0, 115.0, 112.0]
    closes = rising + falling
    opens = [c - 0.2 for c in rising] + [119.0, 116.0, 113.0]
    highs = [c + 0.5 for c in rising] + [119.2, 116.5, 113.5]
    lows = [c - 0.5 for c in rising] + [117.0, 114.5, 111.5]
    b = {"A": bars("A", opens, highs, lows, closes, atr=1.0, atr22=[1.0] * len(closes))}
    plan = LongTermPlan("L3", "chandelier", chandelier_atr=3.0)
    trade = _long_term(b, plan, signal_at(len(closes), 1)).trades[0]

    assert trade.exit_reason == "stop"
    peak_high = max(highs[:20])                  # 119.5 on the last rising bar
    stop = peak_high - 3.0                       # 116.5, raised as the high rose
    # Bar 20's low (117.0) stays above it; bar 21 opens at 116.0, already
    # below, so the stop fills at that open.
    assert trade.fills[-1].when == b["A"].dates[21]
    assert trade.fills[-1].price == pytest.approx(min(opens[21], stop))


def test_curve_stats():
    curve = pd.Series([100.0, 120.0, 90.0, 110.0],
                      index=[date(2024, 1, 1), date(2024, 4, 1), date(2024, 8, 1), date(2025, 1, 1)])
    stats = curve_stats(curve, 100.0)
    assert stats["max_drawdown_pct"] == pytest.approx(-25.0)
    assert stats["cagr_pct"] == pytest.approx(10.0, abs=0.2)
