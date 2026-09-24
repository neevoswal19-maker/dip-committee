"""Position sizing tests.

The arithmetic is checked against hand-computed values, and the safety
properties are checked as invariants: no edge means no position, a volatile
stock at high conviction sizes smaller than a calm one at lower conviction,
and portfolio heat never exceeds its ceiling however many positions are open.
"""

from __future__ import annotations

import pytest

from src.config import load_config
from src.strategy import sizing as sz


class _Override:
    """The real config with a few keys replaced."""

    def __init__(self, base, **overrides):
        self._base, self._over = base, overrides

    def get(self, key, default=None):
        return self._over[key] if key in self._over else self._base.get(key, default)


@pytest.fixture
def cfg():
    # Most tests here exercise the ATR overlay - volatile stocks sized
    # smaller - which is what `stop_rule: atr` does. The -25% stop now in
    # config.yaml is covered by TestPercentStop below.
    return _Override(load_config(), **{"sizing.stop_rule": "atr"})


@pytest.fixture
def portfolio():
    return sz.PortfolioState(capital=1_000_000.0)


class TestKellyFormula:
    def test_matches_hand_computation(self):
        """p=0.60, b=2.0 -> f* = (2.0*0.60 - 0.40) / 2.0 = 0.40"""
        assert sz.kelly_fraction(0.60, 2.0) == pytest.approx(0.40)

    def test_break_even_edge_is_zero(self):
        """p=1/3 at b=2.0 is exactly break-even: (2*0.3333 - 0.6667)/2 = 0"""
        assert sz.kelly_fraction(1 / 3, 2.0) == pytest.approx(0.0, abs=1e-9)

    def test_negative_edge_collapses_to_zero(self):
        # We only ever buy, so a negative Kelly means "do not trade",
        # not "take the other side".
        assert sz.kelly_fraction(0.30, 1.0) == 0.0
        assert sz.kelly_fraction(0.10, 0.5) == 0.0

    def test_zero_payoff_is_safe(self):
        assert sz.kelly_fraction(0.90, 0.0) == 0.0


class TestEdgeEstimation:
    def test_cold_starts_without_history(self, cfg):
        edge = sz.estimate_edge("balanced", cfg, closed_trades=[])
        assert edge.source == "cold_start"
        assert edge.sample_size == 0
        # Read the prior from config rather than pinning a literal: the
        # learner rewrites these values, and the behaviour under test is
        # "falls back to the configured prior", not any particular number.
        assert edge.win_probability == pytest.approx(cfg.get("sizing.cold_start.balanced.p"))
        assert edge.payoff_ratio == pytest.approx(cfg.get("sizing.cold_start.balanced.b"))

    def test_uses_the_ledger_once_there_is_enough_history(self, cfg):
        trades = (
            [{"conviction_band": "balanced", "return_pct": 20.0}] * 15
            + [{"conviction_band": "balanced", "return_pct": -10.0}] * 10
        )
        edge = sz.estimate_edge("balanced", cfg, trades)

        assert edge.source == "ledger"
        assert edge.sample_size == 25
        assert edge.win_probability == pytest.approx(0.60)
        assert edge.payoff_ratio == pytest.approx(2.0)

    def test_blends_prior_and_evidence_on_a_short_history(self, cfg):
        trades = (
            [{"conviction_band": "balanced", "return_pct": 20.0}] * 6
            + [{"conviction_band": "balanced", "return_pct": -10.0}] * 4
        )
        edge = sz.estimate_edge("balanced", cfg, trades)

        assert edge.source == "blended"
        # 10 of the 20 trades needed, so exactly halfway between the
        # configured prior and the 0.60 observed in this sample.
        prior = cfg.get("sizing.cold_start.balanced.p")
        assert edge.win_probability == pytest.approx(prior * 0.5 + 0.60 * 0.5)

    def test_ignores_other_bands(self, cfg):
        trades = [{"conviction_band": "aggressive", "return_pct": 50.0}] * 30
        edge = sz.estimate_edge("balanced", cfg, trades)
        assert edge.source == "cold_start"

    def test_all_wins_falls_back_to_the_prior(self, cfg):
        """A run with no losses cannot tell us the payoff ratio."""
        trades = [{"conviction_band": "balanced", "return_pct": 15.0}] * 30
        edge = sz.estimate_edge("balanced", cfg, trades)
        assert edge.source == "cold_start"

    def test_expectancy_sign(self, cfg):
        good = sz.EdgeEstimate(0.60, 2.0, 50, "ledger", "balanced")
        bad = sz.EdgeEstimate(0.30, 1.0, 50, "ledger", "balanced")
        assert good.expectancy > 0
        assert bad.expectancy < 0


class TestBandSelection:
    def test_conviction_picks_the_band(self, cfg):
        assert sz.select_band(85, cfg)[0] == "aggressive"
        assert sz.select_band(70, cfg)[0] == "balanced"
        assert sz.select_band(50, cfg, win_probability=0.60)[0] == "conservative"
        assert sz.select_band(30, cfg)[0] is None

    def test_red_flags_step_down_rather_than_reject(self, cfg):
        """High conviction plus a red flag is still ownable, just not heavily."""
        band, notes = sz.select_band(90, cfg, has_red_flags=True)
        assert band == "balanced"
        assert any("red flag" in n for n in notes)

    def test_bull_bear_disagreement_steps_down(self, cfg):
        band, notes = sz.select_band(90, cfg, bull_bear_agree=False)
        assert band == "balanced"
        assert any("disagree" in n for n in notes)

    def test_conservative_needs_odds_in_our_favour(self, cfg):
        assert sz.select_band(50, cfg, win_probability=0.50)[0] is None
        assert sz.select_band(50, cfg, win_probability=0.60)[0] == "conservative"


class TestSizePosition:
    def test_forensic_veto_overrides_everything(self, cfg, portfolio):
        decision = sz.size_position(
            symbol="X", conviction=95.0, price=100.0, atr=2.0,
            cfg=cfg, portfolio=portfolio, forensics_veto=True,
        )
        assert decision.recommendation == "NO_BUY"
        assert not decision.is_buy
        assert any("forensic" in r.lower() for r in decision.rejections)

    def test_no_edge_means_no_position_however_good_the_report(self, cfg, portfolio):
        """The plan's hard rule: f* <= 0 produces NO BUY at any conviction."""
        losing = (
            [{"conviction_band": "aggressive", "return_pct": 5.0}] * 6
            + [{"conviction_band": "aggressive", "return_pct": -30.0}] * 14
        )
        decision = sz.size_position(
            symbol="X", conviction=95.0, price=100.0, atr=2.0,
            cfg=cfg, portfolio=portfolio, closed_trades=losing,
        )
        assert decision.recommendation == "NO_BUY"
        assert decision.raw_kelly == 0.0

    def test_low_conviction_is_rejected(self, cfg, portfolio):
        decision = sz.size_position(
            symbol="X", conviction=20.0, price=100.0, atr=2.0, cfg=cfg, portfolio=portfolio
        )
        assert decision.recommendation == "NO_BUY"

    def test_high_conviction_produces_an_aggressive_position(self, cfg, portfolio):
        decision = sz.size_position(
            symbol="X", conviction=90.0, price=100.0, atr=1.0, cfg=cfg, portfolio=portfolio
        )
        assert decision.recommendation == "AGGRESSIVE"
        assert decision.is_buy
        assert decision.total_value > 0

    def test_volatility_shrinks_size_at_equal_conviction(self, cfg, portfolio):
        """The ATR overlay's guarantee, stated correctly.

        Across bands the comparison is not meaningful, because a higher
        conviction band deliberately carries a larger risk budget (2.5% vs
        1.5%) and that can outweigh higher volatility. Held at one
        conviction, though, a jumpy stock must always size smaller than a
        calm one - otherwise the overlay is decorative.
        """
        volatile = sz.size_position(
            symbol="VOL", conviction=85.0, price=100.0, atr=8.0, cfg=cfg, portfolio=portfolio
        )
        calm = sz.size_position(
            symbol="CALM", conviction=85.0, price=100.0, atr=0.8, cfg=cfg, portfolio=portfolio
        )
        assert volatile.total_value < calm.total_value
        assert volatile.binding_constraint.startswith("ATR")

    def test_risk_stays_within_budget_however_volatile(self, cfg, portfolio):
        """What must hold across bands: rupee risk never exceeds the band."""
        for conviction, budget in ((85.0, 2.5), (65.0, 1.5), (50.0, 0.75)):
            for atr in (0.5, 2.0, 8.0, 20.0):
                decision = sz.size_position(
                    symbol="X", conviction=conviction, price=100.0, atr=atr,
                    cfg=cfg, portfolio=portfolio,
                )
                if decision.is_buy:
                    assert decision.risk_pct_of_capital <= budget + 0.05, (
                        f"conviction {conviction} / ATR {atr} risked "
                        f"{decision.risk_pct_of_capital:.2f}% against a {budget}% budget"
                    )

    def test_reports_which_constraint_bound_the_size(self, cfg, portfolio):
        decision = sz.size_position(
            symbol="X", conviction=90.0, price=100.0, atr=0.1, cfg=cfg, portfolio=portfolio
        )
        assert decision.binding_constraint
        assert decision.binding_constraint in ("Kelly", "per-stock cap of 18.0%")

    def test_per_stock_cap_is_respected(self, cfg, portfolio):
        decision = sz.size_position(
            symbol="X", conviction=90.0, price=100.0, atr=0.05, cfg=cfg, portfolio=portfolio
        )
        assert decision.pct_of_capital <= 18.0 + 1e-6

    def test_sector_cap_is_respected(self, cfg):
        portfolio = sz.PortfolioState(
            capital=1_000_000.0,
            open_positions=2,
            holdings={"A": 200_000.0},
            sector_exposure={"Banking": 230_000.0},
        )
        decision = sz.size_position(
            symbol="HDFCBANK", conviction=90.0, price=100.0, atr=1.0,
            cfg=cfg, portfolio=portfolio, sector="Banking",
        )
        # 25% cap on a 1,000,000 book leaves 20,000 of headroom.
        assert decision.total_value <= 20_000 + 100

    def test_full_book_is_rejected(self, cfg):
        portfolio = sz.PortfolioState(capital=1_000_000.0, open_positions=15)
        decision = sz.size_position(
            symbol="X", conviction=90.0, price=100.0, atr=1.0, cfg=cfg, portfolio=portfolio
        )
        assert decision.recommendation == "NO_BUY"
        assert any("limit" in r for r in decision.rejections)

    def test_heat_ceiling_blocks_a_new_position(self, cfg):
        portfolio = sz.PortfolioState(capital=1_000_000.0, open_positions=5, open_risk=60_000.0)
        decision = sz.size_position(
            symbol="X", conviction=90.0, price=100.0, atr=1.0, cfg=cfg, portfolio=portfolio
        )
        assert decision.recommendation == "NO_BUY"
        assert any("heat" in r for r in decision.rejections)

    def test_heat_is_never_exceeded_across_a_full_book(self, cfg):
        """The invariant from the plan: heat stays under 6% over 15 positions."""
        portfolio = sz.PortfolioState(capital=1_000_000.0)
        max_heat = cfg.get("sizing.constraints.max_portfolio_heat_pct")

        for i in range(15):
            decision = sz.size_position(
                symbol=f"S{i}", conviction=90.0, price=100.0, atr=2.0,
                cfg=cfg, portfolio=portfolio, sector=f"Sector{i}",
            )
            if not decision.is_buy:
                break
            portfolio.open_positions += 1
            portfolio.holdings[f"S{i}"] = decision.total_value
            portfolio.sector_exposure[f"Sector{i}"] = decision.total_value
            portfolio.open_risk += decision.risk_amount

            assert portfolio.heat_pct <= max_heat + 1e-6, (
                f"heat {portfolio.heat_pct:.2f}% exceeded the {max_heat}% ceiling "
                f"after {portfolio.open_positions} positions"
            )

    def test_tiny_position_is_rejected_rather_than_placed(self, cfg):
        portfolio = sz.PortfolioState(capital=20_000.0)
        decision = sz.size_position(
            symbol="X", conviction=50.0, price=100.0, atr=5.0, cfg=cfg, portfolio=portfolio
        )
        assert decision.recommendation == "NO_BUY"
        assert any("minimum" in r for r in decision.rejections)

    def test_cannot_deploy_more_cash_than_exists(self, cfg):
        portfolio = sz.PortfolioState(
            capital=1_000_000.0, open_positions=1, holdings={"A": 970_000.0}
        )
        decision = sz.size_position(
            symbol="X", conviction=90.0, price=100.0, atr=1.0, cfg=cfg, portfolio=portfolio
        )
        assert decision.total_value <= portfolio.cash + 1e-6

    def test_stop_sits_below_entry_by_the_atr_multiple(self, cfg, portfolio):
        decision = sz.size_position(
            symbol="X", conviction=70.0, price=100.0, atr=2.0, cfg=cfg, portfolio=portfolio
        )
        # 2.5 x ATR of 2.0 = 5.0 below a price of 100.
        assert decision.stop_price == pytest.approx(95.0)

    def test_risk_matches_the_band_allowance(self, cfg, portfolio):
        decision = sz.size_position(
            symbol="X", conviction=70.0, price=100.0, atr=2.0, cfg=cfg, portfolio=portfolio
        )
        # Balanced allows 1.5%; the ATR overlay should land at or under it.
        assert decision.risk_pct_of_capital <= 1.5 + 0.05

    def test_missing_atr_degrades_without_crashing(self, cfg, portfolio):
        decision = sz.size_position(
            symbol="X", conviction=70.0, price=100.0, atr=0.0, cfg=cfg, portfolio=portfolio
        )
        assert decision.is_buy
        assert decision.stop_price is None
        assert any("ATR" in a for a in decision.adjustments)


class TestTranches:
    def test_splits_into_the_configured_tranches(self, cfg, portfolio):
        decision = sz.size_position(
            symbol="X", conviction=70.0, price=100.0, atr=2.0, cfg=cfg, portfolio=portfolio
        )
        assert len(decision.tranches) == 3
        assert sum(t.pct_of_position for t in decision.tranches) == pytest.approx(100.0)

    def test_first_tranche_is_at_the_signal_price(self, cfg, portfolio):
        decision = sz.size_position(
            symbol="X", conviction=70.0, price=100.0, atr=2.0, cfg=cfg, portfolio=portfolio
        )
        first = decision.tranches[0]
        assert first.trigger == "signal"
        assert first.trigger_price == pytest.approx(100.0)
        assert first.pct_of_position == pytest.approx(50.0)

    def test_second_tranche_waits_for_further_weakness(self, cfg, portfolio):
        decision = sz.size_position(
            symbol="X", conviction=70.0, price=100.0, atr=2.0, cfg=cfg, portfolio=portfolio
        )
        second = decision.tranches[1]
        assert second.trigger == "drop_pct"
        assert second.trigger_price == pytest.approx(93.0)  # 7% below 100

    def test_narrative_names_the_binding_constraint(self, cfg, portfolio):
        decision = sz.size_position(
            symbol="X", conviction=70.0, price=100.0, atr=2.0, cfg=cfg, portfolio=portfolio
        )
        assert "Bound by" in decision.narrative
        assert "BALANCED" in decision.narrative


class TestPercentStop:
    """The long-term stop chosen by the research: 25% below the entry."""

    @pytest.fixture
    def pct_cfg(self):
        return load_config()

    def test_config_uses_the_researched_stop(self, pct_cfg):
        assert pct_cfg.get("sizing.stop_rule") == "pct"
        assert pct_cfg.get("sizing.stop_pct") == 25.0

    def test_stop_sits_25_percent_below_entry(self, pct_cfg, portfolio):
        decision = sz.size_position(
            symbol="X", conviction=70.0, price=100.0, atr=2.0, cfg=pct_cfg, portfolio=portfolio
        )
        assert decision.stop_price == pytest.approx(75.0)

    def test_size_is_built_on_the_real_stop(self, pct_cfg, portfolio):
        """Risk at the -25% stop must stay inside the band's budget.

        Sizing to a tighter stop than the one used would understate the risk
        several times over.
        """
        decision = sz.size_position(
            symbol="X", conviction=70.0, price=100.0, atr=2.0, cfg=pct_cfg, portfolio=portfolio
        )
        assert decision.is_buy
        assert decision.risk_amount == pytest.approx(decision.total_shares * 25.0, rel=1e-6)
        assert decision.risk_pct_of_capital <= 1.5 + 0.05

    def test_no_atr_is_no_problem_for_a_percent_stop(self, pct_cfg, portfolio):
        decision = sz.size_position(
            symbol="X", conviction=70.0, price=100.0, atr=0.0, cfg=pct_cfg, portfolio=portfolio
        )
        assert decision.stop_price == pytest.approx(75.0)
