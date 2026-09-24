"""Exit doctrine tests.

The priority ordering is the important property: a broken thesis must
outrank a tax deadline, and a tax deadline must not be able to hold a
position that should be sold outright.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.config import load_config
from src.strategy import exit as ex


@pytest.fixture
def cfg():
    return load_config()


@pytest.fixture
def position():
    return ex.Position(
        symbol="TEST",
        entry_date=date.today() - timedelta(days=200),
        avg_entry_price=100.0,
        quantity=100,
        stop_price=88.0,
    )


class TestDoctrine:
    def test_is_written_at_entry(self, cfg):
        doctrine = ex.build_doctrine("TEST", 100.0, cfg, stop_price=90.0)

        assert doctrine.symbol == "TEST"
        assert len(doctrine.targets) == 2
        assert doctrine.targets[0]["price"] == pytest.approx(125.0)   # +25%
        assert doctrine.targets[1]["price"] == pytest.approx(150.0)   # +50%
        assert doctrine.hard_stop_pct == -25.0
        assert len(doctrine.thesis_break_rules) >= 5

    def test_states_the_long_term_tax_date(self, cfg):
        entry = date(2026, 1, 15)
        doctrine = ex.build_doctrine("TEST", 100.0, cfg, entry_date=entry)
        assert (entry + timedelta(days=366)).isoformat() in doctrine.tax_note

    def test_describe_is_human_readable(self, cfg):
        text = ex.build_doctrine("TEST", 100.0, cfg, stop_price=90.0).describe()
        assert "Trim 25%" in text
        assert "Sell regardless of price" in text


class TestThesisBreak:
    def test_forensics_exits_even_at_a_profit(self, cfg, position):
        thesis = ex.ThesisState(forensics_critical=True)
        signals = ex.evaluate(position, 180.0, cfg, thesis=thesis)   # up 80%
        decision = ex.decide(signals)

        assert decision.action == "EXIT"
        assert decision.rule == "thesis_break"

    def test_insider_selling_exits(self, cfg, position):
        thesis = ex.ThesisState(insider_net_sell_pct=2.5)
        assert ex.decide(ex.evaluate(position, 110.0, cfg, thesis=thesis)).action == "EXIT"

    def test_sustained_institutional_exit_triggers(self, cfg, position):
        thesis = ex.ThesisState(institutional_exit_quarters=2)
        assert ex.decide(ex.evaluate(position, 110.0, cfg, thesis=thesis)).action == "EXIT"

    def test_one_quarter_of_selling_is_not_enough(self, cfg, position):
        thesis = ex.ThesisState(institutional_exit_quarters=1)
        signals = ex.evaluate(position, 110.0, cfg, thesis=thesis)
        assert not any(s.rule == "thesis_break" for s in signals)

    def test_rising_pledge_warns_before_it_breaches(self, cfg, position):
        thesis = ex.ThesisState(promoter_pledge_pct=15.0, promoter_pledge_prev_pct=8.0)
        signals = ex.evaluate(position, 110.0, cfg, thesis=thesis)
        pledge = [s for s in signals if s.detail.get("trigger") == "pledge_rising"]
        assert pledge and pledge[0].action == "REVIEW"

    def test_breached_pledge_exits(self, cfg, position):
        thesis = ex.ThesisState(promoter_pledge_pct=25.0)
        assert ex.decide(ex.evaluate(position, 110.0, cfg, thesis=thesis)).action == "EXIT"


class TestTargets:
    def test_first_target_trims(self, cfg, position):
        signals = ex.evaluate(position, 126.0, cfg)
        trims = [s for s in signals if s.rule == "target"]
        assert trims and trims[0].trim_pct == 25.0

    def test_a_taken_trim_does_not_fire_again(self, cfg, position):
        position.trims_taken = [25.0]
        signals = ex.evaluate(position, 126.0, cfg)
        assert not [s for s in signals if s.rule == "target" and s.detail["target_pct"] == 25.0]

    def test_both_targets_fire_on_a_large_move(self, cfg, position):
        signals = ex.evaluate(position, 160.0, cfg)
        assert len([s for s in signals if s.rule == "target"]) == 2


class TestTrailingStop:
    def test_does_not_activate_before_the_gain_threshold(self, cfg, position):
        position.peak_price = 120.0     # only +20%, activation is +30%
        signals = ex.evaluate(position, 100.0, cfg)
        assert not [s for s in signals if s.rule == "trailing_stop"]

    def test_fires_after_a_twenty_percent_fall_from_the_peak(self, cfg, position):
        position.peak_price = 200.0     # +100%, so the trail is active
        signals = ex.evaluate(position, 159.0, cfg)   # 20.5% below the peak
        trailing = [s for s in signals if s.rule == "trailing_stop"]
        assert trailing and trailing[0].action == "EXIT"

    def test_holds_while_above_the_trail(self, cfg, position):
        position.peak_price = 200.0
        signals = ex.evaluate(position, 185.0, cfg)   # 7.5% below the peak
        trailing = [s for s in signals if s.rule == "trailing_stop"]
        assert all(s.action != "EXIT" for s in trailing)

    def test_update_peak_only_moves_upward(self):
        position = ex.Position("T", date.today(), 100.0, 10, peak_price=150.0)
        assert ex.update_peak(position, 160.0) is True
        assert position.peak_price == 160.0
        assert ex.update_peak(position, 140.0) is False
        assert position.peak_price == 160.0


class _Override:
    def __init__(self, base, **overrides):
        self._base, self._over = base, overrides

    def get(self, key, default=None):
        return self._over[key] if key in self._over else self._base.get(key, default)


class TestHardStop:
    def test_under_the_atr_rule_it_forces_a_review_not_a_sale(self, cfg, position):
        atr_cfg = _Override(cfg, **{"sizing.stop_rule": "atr"})
        signals = ex.evaluate(position, 70.0, atr_cfg)    # -30%
        hard = [s for s in signals if s.rule == "hard_stop"]
        assert hard
        assert hard[0].action == "REVIEW"

    def test_the_researched_25_percent_stop_sells(self, cfg, position):
        assert cfg.get("sizing.stop_rule") == "pct"
        signals = ex.evaluate(position, 74.0, cfg)        # -26%
        hard = [s for s in signals if s.rule == "hard_stop"]
        assert hard and hard[0].action == "EXIT"
        assert not [s for s in signals if s.rule == "initial_stop"]

    def test_nothing_fires_above_the_stop(self, cfg, position):
        signals = ex.evaluate(position, 80.0, cfg)        # -20%
        assert not [s for s in signals if s.rule in ("hard_stop", "initial_stop")]


class TestTax:
    def test_warns_inside_the_window_with_the_rupee_cost(self, cfg):
        position = ex.Position(
            symbol="T",
            entry_date=date.today() - timedelta(days=340),   # 26 days to go
            avg_entry_price=100.0,
            quantity=1000,
        )
        signals = ex.evaluate(position, 150.0, cfg)
        tax = [s for s in signals if s.rule == "ltcg_deadline"]

        assert tax, "must warn inside the 45-day window"
        detail = tax[0].detail
        assert detail["days_to_ltcg"] == 26
        # Profit 50,000: STCG 20% = 10,000. LTCG is nil, since 50,000 is
        # under the 1.25 lakh exemption.
        assert detail["unrealised_profit"] == pytest.approx(50_000.0)
        assert detail["stcg_due"] == pytest.approx(10_000.0)
        assert detail["ltcg_due"] == pytest.approx(0.0)
        assert detail["saving"] == pytest.approx(10_000.0)

    def test_large_profit_uses_the_exemption_correctly(self, cfg):
        position = ex.Position(
            symbol="T",
            entry_date=date.today() - timedelta(days=350),
            avg_entry_price=100.0,
            quantity=5000,
        )
        signals = ex.evaluate(position, 150.0, cfg)
        detail = [s for s in signals if s.rule == "ltcg_deadline"][0].detail

        # Profit 250,000: STCG 50,000. LTCG on (250,000 - 125,000) at 12.5% = 15,625.
        assert detail["stcg_due"] == pytest.approx(50_000.0)
        assert detail["ltcg_due"] == pytest.approx(15_625.0)

    def test_no_warning_once_long_term(self, cfg):
        position = ex.Position("T", date.today() - timedelta(days=400), 100.0, 100)
        assert not [s for s in ex.evaluate(position, 150.0, cfg) if s.rule == "ltcg_deadline"]

    def test_no_warning_on_a_loss(self, cfg):
        position = ex.Position("T", date.today() - timedelta(days=340), 100.0, 100)
        assert not [s for s in ex.evaluate(position, 80.0, cfg) if s.rule == "ltcg_deadline"]

    def test_tax_never_outranks_a_broken_thesis(self, cfg):
        """The ordering that matters most: tax is a cost, a break is a loss."""
        position = ex.Position("T", date.today() - timedelta(days=340), 100.0, 1000)
        thesis = ex.ThesisState(forensics_critical=True)

        decision = ex.decide(ex.evaluate(position, 150.0, cfg, thesis=thesis))
        assert decision.action == "EXIT"
        assert decision.rule == "thesis_break"


class TestValuation:
    def test_expensive_with_growth_intact_is_a_hold(self, cfg, position):
        thesis = ex.ThesisState(pe_vs_median_sd=2.0, growth_decelerating=False)
        valuation = [s for s in ex.evaluate(position, 200.0, cfg, thesis=thesis) if s.rule == "valuation"]
        assert valuation and valuation[0].action == "HOLD"

    def test_expensive_with_growth_slowing_trims(self, cfg, position):
        thesis = ex.ThesisState(pe_vs_median_sd=2.0, growth_decelerating=True)
        valuation = [s for s in ex.evaluate(position, 200.0, cfg, thesis=thesis) if s.rule == "valuation"]
        assert valuation and valuation[0].action == "TRIM"


class TestDataGaps:
    def test_missing_data_reviews_but_never_sells(self, cfg, position):
        thesis = ex.ThesisState(data_complete=False, missing=["shareholding", "insider"])
        signals = ex.evaluate(position, 110.0, cfg, thesis=thesis)
        gaps = [s for s in signals if s.rule == "data_gap"]

        assert gaps and gaps[0].action == "REVIEW"
        assert ex.decide(signals).action != "EXIT", "a quiet feed is not a reason to sell"

    def test_quiet_day_is_an_explicit_hold(self, cfg, position):
        decision = ex.decide(ex.evaluate(position, 105.0, cfg))
        assert decision.action == "HOLD"
        assert "intact" in decision.message


class TestPerLotTax:
    """Staged entries create several lots, each with its own tax clock.

    Under FIFO a sale disposes of the oldest shares first, so the lot worth
    warning about is the oldest one still short-term - not the position as a
    whole, and not an average of its dates.
    """

    def _staged(self, days_ago_list, price=100.0, qty=10):
        """A position built from tranches bought N days ago each."""
        lots = [
            ex.TaxLot(
                quantity=qty,
                trade_date=date.today() - timedelta(days=d),
                cost_per_share=price,
                tranche_label=f"T{i+1}",
            )
            for i, d in enumerate(days_ago_list)
        ]
        return ex.Position(
            symbol="STAGED",
            entry_date=min(l.trade_date for l in lots),
            avg_entry_price=price,
            quantity=qty * len(lots),
            lots=lots,
        )

    def test_each_tranche_has_its_own_ltcg_date(self):
        position = self._staged([400, 340, 280])
        ltcg = [l.days_to_ltcg(366) for l in position.effective_lots()]

        assert ltcg[0] == 0, "the oldest tranche is already long-term"
        assert len(set(ltcg)) == 3, "three tranches, three different clocks"
        assert ltcg[1] < ltcg[2]

    def test_warns_about_the_oldest_short_term_lot(self, cfg):
        # T1 long-term already, T2 26 days away, T3 86 days away.
        position = self._staged([400, 340, 280])
        signals = ex.evaluate(position, 150.0, cfg)
        tax = [s for s in signals if s.rule == "ltcg_deadline"]

        assert tax, "must warn about the lot inside the window"
        detail = tax[0].detail
        assert detail["days_to_ltcg"] == 26
        assert detail["lot_label"] == "T2"
        assert detail["lot_quantity"] == 10

    def test_figures_cover_only_that_lot(self, cfg):
        """Quoting the whole position would overstate what is at stake."""
        position = self._staged([400, 340, 280], price=100.0, qty=10)
        tax = [s for s in ex.evaluate(position, 150.0, cfg) if s.rule == "ltcg_deadline"][0]

        # One lot of 10 at a Rs 50 gain = Rs 500, not the Rs 1,500 the whole
        # 30-share position shows.
        assert tax.detail["unrealised_profit"] == pytest.approx(500.0)
        assert tax.detail["stcg_due"] == pytest.approx(100.0)   # 20% of 500

    def test_reports_how_much_is_already_long_term(self, cfg):
        position = self._staged([400, 340, 280])
        tax = [s for s in ex.evaluate(position, 150.0, cfg) if s.rule == "ltcg_deadline"][0]

        assert tax.detail["long_term_quantity"] == 10
        assert "already long-term" in tax.message

    def test_silent_once_every_lot_is_long_term(self, cfg):
        position = self._staged([500, 450, 400])
        assert not [s for s in ex.evaluate(position, 150.0, cfg) if s.rule == "ltcg_deadline"]

    def test_silent_when_the_nearest_lot_is_far_away(self, cfg):
        position = self._staged([100, 60, 20])
        assert not [s for s in ex.evaluate(position, 150.0, cfg) if s.rule == "ltcg_deadline"]

    def test_a_lot_at_a_loss_raises_no_warning(self, cfg):
        """There is no tax to save on a loss."""
        position = self._staged([340], price=200.0)
        assert not [s for s in ex.evaluate(position, 150.0, cfg) if s.rule == "ltcg_deadline"]

    def test_a_position_without_lots_still_works(self, cfg):
        """Legacy rows have no lot detail and must keep behaving."""
        position = ex.Position(
            symbol="OLD",
            entry_date=date.today() - timedelta(days=340),
            avg_entry_price=100.0,
            quantity=100,
        )
        tax = [s for s in ex.evaluate(position, 150.0, cfg) if s.rule == "ltcg_deadline"]

        assert tax
        assert tax[0].detail["days_to_ltcg"] == 26
        assert tax[0].detail["unrealised_profit"] == pytest.approx(5000.0)
