"""The Valuation Analyst: cheap against its own past, against its peers, on
the right measure for its kind of business."""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest
from sqlalchemy import create_engine

from src import db
from src.agents.equity_desk import FundamentalResearchAnalyst, ValuationAnalyst
from src.config import load_config
from src.data import fundamentals, valuations

CFG = load_config()

FY_ENDS = ["2026-03-31", "2025-03-31", "2024-03-31", "2023-03-31"]


class Ctx:
    """The slice of the evidence pack the bot reads."""

    def __init__(self, fund, price=100.0):
        self._fund = fund
        index = pd.bdate_range("2020-01-01", "2026-09-25")
        self.price_frame = pd.DataFrame({"close": price}, index=index)

    def get(self, name):
        return self._fund if name == "fundamentals" else None

    def reachable(self, name):
        return True


@pytest.fixture
def peers(monkeypatch):
    table = {}
    monkeypatch.setattr(valuations, "sector_medians", lambda sector: table.get(sector, {}))
    return table


def _company(**kw):
    base = {"sector": "Industrials", "pe_trailing": 12.0, "profit_cagr_3y_pct": 15.0,
            "fcf_yield_pct": 5.0, "price_to_book": 3.0,
            # EPS at the last four year-ends: at a price of 100, P/E of 18-22, median 20.
            "eps_history": [[d, e] for d, e in zip(FY_ENDS, [5.5, 5.0, 4.5, 5.0])],
            "bvps_history": []}
    base.update(kw)
    return base


def run(company):
    return ValuationAnalyst(CFG).run(Ctx(company))


def evidence(verdict):
    return {e.field: e.value for e in verdict.evidence}


def test_cheaper_than_its_past_and_its_peers_is_cheap(peers):
    peers["Industrials"] = {"pe": {"median": 22.0, "n": 40}, "fcf_yield": {"median": 3.0, "n": 40}}
    v = run(_company())
    ev = evidence(v)
    assert ev["valuation_label"] == "CHEAP"
    assert ev["own_median"] == pytest.approx(20.0, abs=0.5)
    assert ev["sector_median"] == 22.0
    assert ev["peg"] == pytest.approx(0.8)
    assert v.key_findings[0].startswith("Valuation: CHEAP - P/E 12.0 vs its usual 20")
    assert "sector 22.0" in v.key_findings[0]
    assert v.score > 1.5


def test_dearer_than_both_is_expensive(peers):
    peers["Industrials"] = {"pe": {"median": 22.0, "n": 40}}
    v = run(_company(pe_trailing=40.0, fcf_yield_pct=1.0))
    assert evidence(v)["valuation_label"] == "EXPENSIVE"
    assert v.score < -1.5


def test_a_fast_grower_is_not_punished_for_a_higher_multiple(peers):
    slow = run(_company(pe_trailing=25.0, profit_cagr_3y_pct=5.0))
    fast = run(_company(pe_trailing=25.0, profit_cagr_3y_pct=40.0))
    assert fast.score > slow.score


def test_lenders_are_judged_on_price_to_book(peers):
    peers["Financial Services"] = {"pb": {"median": 3.0, "n": 30}, "pe": {"median": 5.0, "n": 30}}
    bank = _company(sector="Financial Services", price_to_book=1.5, pe_trailing=30.0,
                    bvps_history=[[d, b] for d, b in zip(FY_ENDS, [50.0, 45.0, 40.0, 38.0])])
    v = run(bank)
    ev = evidence(v)
    assert ev["basis"] == "P/B"
    assert ev["current"] == 1.5
    assert ev["peg"] is None and ev["fcf_yield_pct"] is None     # meaningless for lenders
    assert ev["valuation_label"] == "CHEAP"


def test_without_a_sector_table_it_still_judges_against_its_own_past(peers):
    v = run(_company())
    assert evidence(v)["sector_median"] is None
    assert any("No sector median available yet" in f for f in v.key_findings)
    assert evidence(v)["own_median"] is not None


def test_a_loss_maker_says_so(peers):
    v = run(_company(pe_trailing=None, fcf_yield_pct=None,
                     eps_history=[[d, e] for d, e in zip(FY_ENDS, [-2.0, 1.0, 1.5, 2.0])]))
    assert evidence(v)["valuation_label"] == "LOSS-MAKING"
    assert v.score < 0


def test_no_fundamentals_is_blind_not_neutral(peers):
    v = run(None)
    assert v.data_available is False


def test_history_needs_prices_that_reach_back(peers):
    ctx = Ctx(_company())
    ctx.price_frame = ctx.price_frame[ctx.price_frame.index >= "2025-06-01"]
    v = ValuationAnalyst(CFG).run(ctx)
    assert evidence(v)["own_median"] is None      # only one year-end covered


def test_the_fundamental_bot_no_longer_scores_pe():
    """Valuation belongs to one bot; counting P/E twice would double its weight."""
    base = {"roe_pct": 20.0, "debt_to_equity": 0.3, "revenue_cagr_3y_pct": 12.0,
            "profitable_years_of_4": 4, "sector": "Industrials"}

    class Fund:
        def __init__(self, m):
            self.m = m

        def get(self, name):
            return self.m if name == "fundamentals" else None

        def reachable(self, name):
            return True

    cheap = FundamentalResearchAnalyst(CFG).run(Fund({**base, "pe_trailing": 8.0}))
    dear = FundamentalResearchAnalyst(CFG).run(Fund({**base, "pe_trailing": 90.0}))
    # Both must actually have judged - two crashed bots also score the same.
    assert cheap.data_available and dear.data_available
    assert cheap.score > 1.0
    assert cheap.score == dear.score


# --- Data --------------------------------------------------------------------------------


def test_valuation_inputs_from_statements():
    cols = [pd.Timestamp("2026-03-31"), pd.Timestamp("2025-03-31")]
    income = pd.DataFrame([[5.5, 5.0]], index=["Diluted EPS"], columns=cols)
    balance = pd.DataFrame([[1000.0, 900.0], [100.0, 100.0]],
                           index=["Stockholders Equity", "Ordinary Shares Number"], columns=cols)
    cashflow = pd.DataFrame([[50.0, 40.0]], index=["Free Cash Flow"], columns=cols)

    out = fundamentals.valuation_inputs(income, balance, cashflow, market_cap=1000.0)
    assert out["eps_history"] == [["2026-03-31", 5.5], ["2025-03-31", 5.0]]
    assert out["bvps_history"] == [["2026-03-31", 10.0], ["2025-03-31", 9.0]]
    assert out["fcf_yield_pct"] == pytest.approx(5.0)


def test_sector_medians_drop_distortions_and_thin_sectors():
    companies = ([{"sector": "IT", "pe_trailing": pe} for pe in (20, 22, 24, 26, 28)]
                 + [{"sector": "IT", "pe_trailing": 950}]                     # one bad year
                 + [{"sector": "Tiny", "pe_trailing": pe} for pe in (10, 12)])  # too few peers
    rows = valuations.compute(companies)
    it = next(r for r in rows if r["sector"] == "IT" and r["metric"] == "pe")
    assert it["median"] == 24 and it["n"] == 5
    assert not any(r["sector"] == "Tiny" for r in rows)


def test_sector_medians_round_trip(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path/'v.db'}")
    monkeypatch.setattr(db, "_engine", engine)
    db.metadata.create_all(engine)
    rows = valuations.compute([{"sector": "IT", "pe_trailing": pe, "price_to_book": 5.0}
                               for pe in (20, 22, 24, 26, 28)])
    assert valuations.store(rows) == 2
    assert valuations.sector_medians("IT")["pe"]["median"] == 24
    assert valuations.store(rows) == 2                       # replaced, not appended
    assert valuations.sector_medians("Unknown") == {}


# --- Alerts ------------------------------------------------------------------------------


def test_the_summary_says_how_each_candidate_is_valued():
    from datetime import date

    from src.alerts import telegram

    scan = SimpleNamespace(universe_size=501, duration_seconds=500, passed_dip=15,
                           passed_delivery=1, trade_date=date(2026, 9, 25))
    body = telegram.scan_summary(scan, [{"symbol": "INDIANB", "close": 830.0, "drawdown_pct": 16.0,
                                         "conviction": 53.0, "stance": "WATCH", "valuation": "CHEAP"}], CFG)
    assert "WATCH 53, looks cheap" in body


def test_the_buy_alert_carries_the_valuation_headline():
    from src.alerts import telegram

    report = SimpleNamespace(
        symbol="INDIANB", conviction=64.0, sector="Banks", price=830.0, sizing={},
        exit_doctrine={}, market_regime={}, desks=[], red_flags=[], coverage=1.0,
        all_verdicts=[SimpleNamespace(bot_id="valuation_analyst",
                                      key_findings=["Valuation: CHEAP - P/B 1.1 vs its usual 1.4 and sector 1.6."])],
    )
    assert "Valuation: CHEAP - P/B 1.1" in telegram.buy_candidate(report, CFG)
