"""Transaction cost tests.

Every expected figure is worked out longhand in the docstring. These rates
change with each budget, so when a test here fails the first question is
whether config.yaml was updated and the expectation was not.
"""

from __future__ import annotations

import pytest

from src import charges
from src.config import load_config


@pytest.fixture
def cfg():
    return load_config()


class TestBuySide:
    def test_reproduces_a_worked_contract_note(self, cfg):
        """10 shares at Rs 1,000 on Groww delivery. Turnover Rs 10,000.

        Brokerage   min(0.1% of 10,000 = 10, cap 20)      = 10.00
        STT         0.1%  of 10,000                        = 10.00
        Exchange    0.00297% of 10,000                     =  0.30
        SEBI        0.0001%  of 10,000                     =  0.01
        Stamp duty  0.015%   of 10,000 (buy only)          =  1.50
        GST         18% of (10.00 + 0.297 + 0.01)          =  1.86
        DP          none on a purchase                     =  0.00
                                                             -----
                                                             23.67
        """
        result = charges.estimate("BUY", 10, 1000, cfg=cfg)

        assert result.brokerage == pytest.approx(10.00)
        assert result.stt == pytest.approx(10.00)
        assert result.exchange_fee == pytest.approx(0.30, abs=0.01)
        assert result.sebi_fee == pytest.approx(0.01, abs=0.01)
        assert result.stamp_duty == pytest.approx(1.50)
        assert result.gst == pytest.approx(1.86, abs=0.01)
        assert result.dp_charge == 0.0
        assert result.total == pytest.approx(23.67, abs=0.02)

    def test_brokerage_is_capped(self, cfg):
        """0.1% of Rs 1,00,000 is Rs 100, but the cap is Rs 20."""
        assert charges.estimate("BUY", 100, 1000, cfg=cfg).brokerage == pytest.approx(20.0)

    def test_no_dp_charge_on_a_purchase(self, cfg):
        assert charges.estimate("BUY", 50, 500, cfg=cfg).dp_charge == 0.0


class TestSellSide:
    def test_reproduces_a_worked_contract_note(self, cfg):
        """10 shares at Rs 1,200. Turnover Rs 12,000.

        Brokerage   min(12, 20)                            = 12.00
        STT         0.1% of 12,000                          = 12.00
        Exchange    0.00297% of 12,000                      =  0.36
        SEBI                                                =  0.01
        Stamp duty  none on a sale                          =  0.00
        GST         18% of (12.00 + 0.356 + 0.012)          =  2.23
        DP          flat, sale only                         = 20.00
                                                              -----
                                                              46.60
        """
        result = charges.estimate("SELL", 10, 1200, cfg=cfg)

        assert result.stamp_duty == 0.0, "stamp duty is a purchase-side charge"
        assert result.dp_charge == pytest.approx(20.0)
        assert result.total == pytest.approx(46.60, abs=0.02)

    def test_dp_charge_dominates_a_small_sale(self, cfg):
        """The flat DP fee is why tiny positions are not worth taking.

        On a Rs 2,000 sale the fixed Rs 20 is a full percent by itself.
        """
        result = charges.estimate("SELL", 10, 200, cfg=cfg)
        assert result.dp_charge / (10 * 200) * 100 >= 1.0


class TestGstBase:
    def test_gst_excludes_stt_and_stamp_duty(self, cfg):
        """GST applies to service fees only - taxes are not taxed again."""
        result = charges.estimate("BUY", 10, 1000, cfg=cfg)
        expected = (result.brokerage + result.exchange_fee + result.sebi_fee) * 0.18

        assert result.gst == pytest.approx(expected, abs=0.02)
        assert result.gst < (result.stt + result.stamp_duty) * 0.18 + expected


class TestNetAmounts:
    def test_a_purchase_costs_more_than_turnover(self, cfg):
        breakdown = charges.estimate("BUY", 10, 1000, cfg=cfg)
        net = charges.net_amount("BUY", 10, 1000, breakdown.total)

        assert net == pytest.approx(10_000 + breakdown.total)
        assert net > 10_000

    def test_a_sale_returns_less_than_turnover(self, cfg):
        breakdown = charges.estimate("SELL", 10, 1200, cfg=cfg)
        net = charges.net_amount("SELL", 10, 1200, breakdown.total)

        assert net == pytest.approx(12_000 - breakdown.total)
        assert net < 12_000

    def test_effective_price_moves_against_you_both_ways(self, cfg):
        buy = charges.estimate("BUY", 10, 1000, cfg=cfg)
        sell = charges.estimate("SELL", 10, 1000, cfg=cfg)

        assert charges.effective_price("BUY", 10, 1000, buy.total) > 1000
        assert charges.effective_price("SELL", 10, 1000, sell.total) < 1000


class TestEdgeCases:
    def test_zero_turnover_is_free_rather_than_an_error(self, cfg):
        """The form calls this on every keystroke, including empty fields."""
        assert charges.estimate("BUY", 0, 0, cfg=cfg).total == 0.0
        assert charges.estimate("BUY", 10, 0, cfg=cfg).total == 0.0

    def test_zerodha_delivery_is_brokerage_free(self, cfg):
        result = charges.estimate("BUY", 10, 1000, cfg=cfg, broker="zerodha")
        assert result.brokerage == 0.0
        assert result.stt > 0, "statutory charges still apply"

    def test_round_trip_cost_shrinks_with_position_size(self, cfg):
        """Fixed costs make small positions proportionally expensive."""
        small = charges.round_trip_cost_pct(10, 200, cfg=cfg)
        large = charges.round_trip_cost_pct(100, 2000, cfg=cfg)

        assert small > large
        assert large < 0.5
