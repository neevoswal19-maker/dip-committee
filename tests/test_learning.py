"""The self-learning loop, and above all its guardrails.

The weight learner changes the system without asking, so these tests hold it
to the rules it was given: no move without enough evidence, no move beyond
the step limit, no change that fails on data it was not fitted to, and a way
back to any earlier version.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine, select

from src import db
from src.config import load_config
from src.learning import checkpoints, postmortem, proposals, scorecards, weights

CFG = load_config()


@pytest.fixture
def database(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path/'learn.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "_engine", engine)
    db.metadata.create_all(engine)
    return engine


def _series(start: date, values):
    index = pd.bdate_range(start, periods=len(values))
    return pd.Series(values, index=index, dtype=float)


# --- Checkpoints ---------------------------------------------------------------------


def test_measure_waits_until_the_horizon_has_passed():
    closes = _series(date(2026, 1, 1), np.linspace(100, 130, 80))
    assert checkpoints.measure(date(2026, 1, 1), 100.0, 30, closes, None, today=date(2026, 1, 20)) is None


def test_measure_is_excess_over_the_index():
    closes = _series(date(2026, 1, 1), [100.0] * 20 + [110.0] * 60)
    bench = _series(date(2026, 1, 1), [1000.0] * 20 + [1040.0] * 60)
    result = checkpoints.measure(date(2026, 1, 1), 100.0, 30, closes, bench, today=date(2026, 6, 1))
    assert result["forward_return_pct"] == pytest.approx(10.0)
    assert result["benchmark_return_pct"] == pytest.approx(4.0)
    assert result["excess_return_pct"] == pytest.approx(6.0)
    assert result["measured_at"] >= date(2026, 1, 31)


def test_update_records_each_horizon_once(database):
    with db.connection() as conn:
        conn.execute(db.committee_runs.insert().values(
            symbol="INDIANB", run_at=datetime(2026, 1, 1, 8), trade_date=date(2026, 1, 1),
            price_at_run=100.0, strategy="long_term"))
    closes = _series(date(2026, 1, 1), np.linspace(100, 150, 300))
    bench = _series(date(2026, 1, 1), np.linspace(1000, 1100, 300))

    first = checkpoints.update(CFG, today=date(2026, 5, 1), closes_for=lambda s: closes,
                               benchmark_closes=lambda: bench)
    assert first == 2                         # 30 and 90 days have passed; 180 and 365 have not
    again = checkpoints.update(CFG, today=date(2026, 5, 1), closes_for=lambda s: closes,
                               benchmark_closes=lambda: bench)
    assert again == 0


# --- Scorecards ---------------------------------------------------------------------


def _obs(bot, scores, outcomes, desk="equity", role="analyst"):
    return [{"bot_id": bot, "desk": desk, "role": role, "score": s, "excess_return_pct": o}
            for s, o in zip(scores, outcomes)]


def test_scorecards_tell_useful_from_useless():
    rng = np.random.default_rng(3)
    outcomes = rng.normal(0, 10, 60)
    # A score with exactly zero rank correlation: symmetric in the outcome's rank.
    ranks = pd.Series(outcomes).rank().to_numpy()
    unrelated = (ranks - ranks.mean()) ** 2
    obs = (_obs("good", outcomes + rng.normal(0, 3, 60), outcomes)
           + _obs("backwards", -outcomes + rng.normal(0, 3, 60), outcomes)
           + _obs("random", unrelated, outcomes)
           + _obs("new", rng.normal(0, 1, 12), outcomes[:12]))
    verdicts = {c.bot_id: c.verdict for c in scorecards.score(obs, 90, min_n=30)}
    assert verdicts["good"] == "predictive"
    assert verdicts["backwards"] == "misleading"
    assert verdicts["random"] == "noise"
    assert verdicts["new"] == "insufficient"


# --- Weights ---------------------------------------------------------------------------


def _runs(n, *, equity_sign=1.0, news_sign=-1.0, flip_after=None, seed=5):
    """Committee runs where equity predicts outcomes and news predicts them backwards."""
    rng = np.random.default_rng(seed)
    runs = []
    for i in range(n):
        outcome = float(rng.normal(0, 10))
        # After `flip_after`, both desks turn around: equity starts pointing
        # the wrong way and news the right way.
        flipped = flip_after is not None and i >= flip_after
        e_sign = -equity_sign if flipped else equity_sign
        n_sign = -news_sign if flipped else news_sign
        runs.append({
            "run_id": i, "run_at": datetime(2026, 1, 1) + timedelta(days=i), "outcome": outcome,
            "desks": {
                "equity": e_sign * outcome + float(rng.normal(0, 3)),
                "news": n_sign * outcome + float(rng.normal(0, 3)),
                "macro": float(rng.normal(0, 5)),
                "ownership": float(rng.normal(0, 5)),
                "review": float(rng.normal(0, 5)),
            },
        })
    return runs


def test_no_weight_moves_without_enough_evidence(database):
    assert weights.propose(CFG, observations=_runs(20)) is None


def test_weight_moves_toward_the_desk_that_was_right_within_the_limit(database):
    proposal = weights.propose(CFG, observations=_runs(100))
    current, new = proposal.current, proposal.proposed
    assert new["equity"] > current["equity"]
    assert new["news"] < current["news"]
    for desk in current:
        assert 0.8 - 1e-3 <= new[desk] / current[desk] <= 1.2 + 1e-3, desk     # the 20% step limit
    assert new["equity"] == pytest.approx(current["equity"] * 1.2, abs=1e-3)   # capped, not +475%
    assert proposal.passed_gate


def test_a_change_that_fails_on_recent_data_is_recorded_not_applied(database):
    # Equity predicts well for 70 runs, then turns backwards on the held-out 30.
    proposal = weights.propose(CFG, observations=_runs(100, flip_after=70))
    assert proposal.proposed["equity"] > proposal.current["equity"]
    assert not proposal.passed_gate

    weights.seed(CFG)
    version = weights.apply(CFG, proposal)
    assert version is not None
    assert weights.active_weights(CFG) == weights.config_weights(CFG)     # unchanged
    history = weights.history()
    assert history[0]["id"] == version and not history[0]["is_active"]


def test_an_applied_change_can_be_undone(database):
    seed = weights.seed(CFG)
    proposal = weights.propose(CFG, observations=_runs(100))
    weights.apply(CFG, proposal)
    assert weights.active_weights(CFG) == pytest.approx(proposal.proposed)

    weights.restore(seed)
    assert weights.active_weights(CFG) == pytest.approx(weights.config_weights(CFG))
    assert weights.history()[0]["source"] == "rollback"


def test_a_dry_run_changes_nothing(database):
    weights.seed(CFG)
    proposal = weights.propose(CFG, observations=_runs(100))
    assert weights.apply(CFG, proposal, dry_run=True) is None
    assert len(weights.history()) == 1


def test_the_committee_uses_the_weights_it_is_given():
    from src.agents import cmio
    from tests.test_agents import desks_all_at

    desks = desks_all_at(2.0)
    for d in desks:
        if d.desk == "news":
            d.score = -5.0
    news_heavy = {"news": 1.0, "equity": 0.01, "macro": 0.01, "ownership": 0.01, "review": 0.01}
    low = cmio.decide("T", desks, price=100.0, atr=2.0, sector=None, cfg=CFG, desk_weights=news_heavy)
    normal = cmio.decide("T", desks, price=100.0, atr=2.0, sector=None, cfg=CFG)
    assert low.conviction < normal.conviction


# --- Post-mortems ---------------------------------------------------------------------------


def _trade(**kw):
    base = {"id": 1, "symbol": "INDIANB", "entry_date": date(2026, 1, 1), "exit_date": date(2026, 2, 13),
            "entry_price": 100.0, "return_pct": 12.0, "mfe_pct": 30.0, "holding_days": 43,
            "exit_reason": "target", "committee_run_id": None}
    base.update(kw)
    return base


def test_trailing_exit():
    closes = pd.Series([100, 110, 120, 105, 95.0])
    assert postmortem.trailing_exit(closes, 100.0, 10.0) == pytest.approx(5.0)    # 105 < 120 * 0.9
    assert postmortem.trailing_exit(closes, 100.0, 25.0) == pytest.approx(-5.0)   # never triggered; last


def test_a_winner_that_left_money_on_the_table_says_so():
    path = list(np.linspace(100, 130, 30)) + list(np.linspace(130, 112, 20)) + [112.0] * 60
    closes = _series(date(2026, 1, 1), path)
    rv = postmortem.review(_trade(), closes, {"equity": 2.0, "news": -1.0})
    assert rv.outcome == "win"
    assert rv.left_on_table == pytest.approx(18.0)
    assert rv.captured == pytest.approx(0.4)
    assert "10% trailing stop" in rv.alternatives
    assert rv.desks_right == ["equity"] and rv.desks_wrong == ["news"]
    exit_lesson = next(text for category, text in rv.lessons if category == "exit")
    assert "18.0 points left" in exit_lesson


def test_a_loss_that_was_once_a_gain_is_an_exit_lesson():
    closes = _series(date(2026, 1, 1), [100, 108, 104, 95, 90] + [90.0] * 80)
    rv = postmortem.review(_trade(return_pct=-10.0, mfe_pct=8.0, exit_reason="stop"), closes, {})
    assert rv.outcome == "loss"
    assert rv.lessons[0][0] == "exit"
    assert "gain that turned into a loss" in rv.lessons[0][1]


def test_each_closed_trade_is_reviewed_once(database):
    with db.connection() as conn:
        conn.execute(db.trades.insert().values(
            symbol="INDIANB", entry_date=date(2026, 1, 1), exit_date=date(2026, 2, 13), entry_price=100.0,
            exit_price=112.0, quantity=10, return_pct=12.0, holding_days=43, is_win=True, mfe_pct=30.0,
            exit_reason="target"))
    closes = _series(date(2026, 1, 1), list(np.linspace(100, 130, 30)) + [112.0] * 80)
    assert len(postmortem.run(CFG, closes_for=lambda s: closes)) == 1
    assert postmortem.run(CFG, closes_for=lambda s: closes) == []
    with db.connection() as conn:
        assert conn.execute(select(db.lessons)).fetchall()


# --- Proposals ----------------------------------------------------------------------------


def _evidence(n, edge, better_share=1.0):
    out = []
    for i in range(n):
        gain = edge if i < n * better_share else -1.0
        out.append({"return_pct": 5.0, "alternatives": {"10% trailing stop": 5.0 + gain,
                                                         "20% trailing stop": 5.0}})
    return out


def test_a_clear_pattern_across_enough_trades_becomes_a_proposal():
    found = proposals.find(CFG, evidence=_evidence(10, edge=4.0))
    assert [(f["config_path"], f["proposed_value"]) for f in found] == [("exit.trailing_stop.trail_pct", "10.0")]


def test_too_few_trades_or_a_weak_edge_proposes_nothing():
    assert proposals.find(CFG, evidence=_evidence(5, edge=10.0)) == []
    assert proposals.find(CFG, evidence=_evidence(10, edge=1.0)) == []
    assert proposals.find(CFG, evidence=_evidence(10, edge=4.0, better_share=0.5)) == []


def test_proposals_are_never_filed_twice(database):
    found = proposals.find(CFG, evidence=_evidence(10, edge=4.0))
    assert proposals.record(found) == 1
    assert proposals.record(found) == 0


def test_set_scalar_changes_one_value_and_keeps_the_comments():
    text = open("config.yaml", encoding="utf-8").read()
    edited = proposals.set_scalar(text, "exit.trailing_stop.trail_pct", "15.0")
    changed = [(a, b) for a, b in zip(text.split("\n"), edited.split("\n")) if a != b]
    assert len(changed) == 1
    before, after = changed[0]
    assert "trail_pct: 15.0" in after
    assert "#" in before and before.split("#", 1)[1] == after.split("#", 1)[1]

    import yaml
    assert yaml.safe_load(edited)["exit"]["trailing_stop"]["trail_pct"] == 15.0


def test_set_scalar_only_matches_at_the_right_depth():
    text = "a:\n  b:\n    c: 1\nb:\n  c: 2\n"
    assert proposals.set_scalar(text, "b.c", "9") == "a:\n  b:\n    c: 1\nb:\n  c: 9\n"
    with pytest.raises(KeyError):
        proposals.set_scalar(text, "a.c", "9")
