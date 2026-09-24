"""The pass/fail rules of the strategy research.

They were fixed before any result was seen. These tests hold them to that:
a strategy with a great win rate but no profit, or one that only works in
the years it was chosen on, must fail.
"""

from __future__ import annotations

from src.strategy import research, swing


def _stats(n=200, win=0.65, pf=1.5, exp=0.4):
    return {"trades": n, "win_rate": win, "profit_factor": pf, "expectancy_pct": exp,
            "avg_win_pct": 1.5, "avg_loss_pct": -2.0}


def _verdict(**overrides):
    base = dict(
        rule=swing.RULES["rsi2"], plan=swing.ExitPlan(None, None, 10),
        train=_stats(), test=_stats(n=120), test_same_close=_stats(n=120),
        next50=_stats(win=0.58, pf=1.2),
        portfolio={"max_drawdown_pct": -12.0}, benchmark={"max_drawdown_pct": -18.0},
        grid=[],
    )
    base.update(overrides)
    return research.judge(research.Verdict(**base))


def test_a_strategy_meeting_every_criterion_passes():
    v = _verdict()
    assert v.passed, v.reasons


def test_a_high_win_rate_that_loses_money_fails():
    """The trap the research found: wins often, loses big."""
    v = _verdict(test=_stats(n=120, win=0.72, pf=0.9, exp=-0.1))
    assert not v.passed
    assert any("profit factor" in r for r in v.reasons)
    assert any("loses" in r for r in v.reasons)


def test_it_must_hold_up_after_the_years_it_was_chosen_on():
    v = _verdict(test=_stats(n=120, win=0.52))
    assert not v.passed
    assert any("2022+" in r and "win rate" in r for r in v.reasons)


def test_it_must_hold_on_stocks_it_never_saw():
    v = _verdict(next50=_stats(win=0.50, pf=1.0))
    assert not v.passed
    assert any("Next 50" in r for r in v.reasons)


def test_a_deeper_fall_than_the_index_fails():
    v = _verdict(portfolio={"max_drawdown_pct": -25.0})
    assert not v.passed
    assert any("drawdown" in r for r in v.reasons)


def test_too_few_trades_cannot_pass():
    v = _verdict(test=_stats(n=20))
    assert not v.passed
    assert any("only 20 trades" in r for r in v.reasons)


def test_long_term_keeps_the_current_plan_unless_beaten_on_both_counts():
    def plan(cagr, dd):
        return {"test_curve": {"cagr_pct": cagr, "max_drawdown_pct": dd}}

    # Faster but falls further: not good enough.
    choice, _ = research.choose_long_term({"L1": plan(12, -15), "L2": plan(14, -20), "L3": plan(10, -10)})
    assert choice == "L1"

    # Faster and shallower: replaces it.
    choice, reason = research.choose_long_term({"L1": plan(12, -15), "L2": plan(13, -14), "L3": plan(11, -9)})
    assert choice == "L2"
    assert "both" in reason
