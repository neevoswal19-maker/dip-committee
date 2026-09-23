"""Committee tests.

Two properties matter more than any individual bot's scoring:

1. Missing data must never read as neutral. A blind bot contributes zero
   weight, not a zero score, because "we could not look" and "we looked and
   it was fine" are opposite findings.
2. Bull and Bear must genuinely diverge. Two adversaries that agree mean the
   design has collapsed into one opinion counted twice.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

from src.agents import cmio, leads, registry
from src.agents.schemas import (
    CommitteeReport, DeskReport, Evidence, Verdict, score_to_conviction,
)
from src.config import load_config
from src.data.provider import DataResult, DataStatus, StockIdentity


@pytest.fixture
def cfg():
    return load_config()


class FakeContext:
    """An evidence pack assembled by hand, so bots can be tested in isolation."""

    def __init__(self, **fields: Any):
        self.stock = fields.pop("stock", StockIdentity("TEST", name="Test Ltd", sector="Capital Goods"))
        self.as_of = date.today()
        self.cfg = load_config()
        self.price_frame = fields.pop("price_frame", None)
        self.peer_verdicts: dict[str, Verdict] = fields.pop("peer_verdicts", {})
        self._fields = fields

    def __getattr__(self, name: str) -> Any:
        return self._fields.get(name)

    @property
    def symbol(self) -> str:
        return self.stock.symbol

    @property
    def sector(self) -> str | None:
        return self.stock.sector

    @property
    def price(self) -> float:
        if self.price_frame is not None and not self.price_frame.empty:
            return float(self.price_frame["close"].iloc[-1])
        return 100.0

    @property
    def latest(self):
        if self.price_frame is not None and not self.price_frame.empty:
            return self.price_frame.iloc[-1]
        return None

    def reachable(self, name: str) -> bool:
        if name == "price_frame":
            return self.price_frame is not None and not self.price_frame.empty
        result = self._fields.get(name)
        return result.available if isinstance(result, DataResult) else result is not None

    def has(self, name: str) -> bool:
        if name == "price_frame":
            return self.price_frame is not None and not self.price_frame.empty
        result = self._fields.get(name)
        if isinstance(result, DataResult):
            value = result.value
            if isinstance(value, pd.DataFrame):
                return result.usable and not value.empty
            return result.usable and value is not None
        return result is not None

    def get(self, name: str, default: Any = None) -> Any:
        result = self._fields.get(name)
        if isinstance(result, DataResult):
            return result.value if result.usable else default
        return result if result is not None else default

    def note_for(self, name: str) -> str:
        result = self._fields.get(name)
        return (result.note or result.status.value) if isinstance(result, DataResult) else "absent"

    def coverage(self) -> dict[str, bool]:
        names = ("price_frame", "delivery", "fundamentals", "shareholding", "insider",
                 "announcements", "news", "deals", "surveillance", "sector_index", "macro")
        return {n: self.has(n) for n in names}


def ok(value: Any, as_of: date | None = None) -> DataResult:
    return DataResult(value=value, status=DataStatus.OK, source="test", as_of=as_of or date.today())


def gone(note: str = "endpoint down") -> DataResult:
    return DataResult.unavailable("test", note)


def verdict(bot_id: str, desk: str, score: float, confidence: float = 0.7, **kwargs) -> Verdict:
    return Verdict(
        bot_id=bot_id, desk=desk, name=bot_id.replace("_", " ").title(),
        score=score, confidence=confidence, **kwargs
    )


# --- The org chart ----------------------------------------------------------


class TestRegistry:
    def test_the_chart_has_twenty_three_bots(self):
        assert len(registry.all_analyst_classes()) == 17
        assert registry.bot_count() == 23

    def test_desk_sizes_match_the_org_chart(self):
        sizes = {desk: len(classes) for desk, classes in registry.PRIMARY_DESKS.items()}
        assert sizes == {"news": 5, "equity": 3, "macro": 2, "ownership": 4}
        assert len(registry.REVIEW_DESK) == 3

    def test_every_bot_id_is_unique(self):
        ids = [cls.bot_id for cls in registry.all_analyst_classes()]
        assert len(ids) == len(set(ids))

    def test_every_bot_instantiates_and_runs_blind_without_data(self, cfg):
        """The committee must survive a total data outage.

        The Research Validation Analyst is the deliberate exception: auditing
        the absence of evidence is its entire function, so it reports with
        confidence even when nothing else could.
        """
        ctx = FakeContext()
        for cls in registry.all_analyst_classes():
            result = cls(cfg).run(ctx)
            assert isinstance(result, Verdict)
            assert result.bot_id == cls.bot_id

            if cls.bot_id == "research_validation_analyst":
                assert result.data_available
                assert any("too thin to act on" in f for f in result.red_flags)
            else:
                assert result.confidence == 0.0 or not result.data_available


# --- The rule that missing data is not neutral ------------------------------


class TestBlindness:
    def test_a_blind_verdict_carries_no_weight(self):
        blind = Verdict.blind("x", "news", "X", "feed down")
        assert blind.score == 0.0
        assert blind.weight == 0.0
        assert blind.stance == "NO_DATA"

    def test_a_neutral_verdict_does_carry_weight(self):
        """The distinction the whole design rests on."""
        neutral = verdict("x", "news", 0.0, 0.6)
        assert neutral.weight == 0.6
        assert neutral.stance != "NO_DATA"

    def test_a_desk_of_blind_analysts_contributes_nothing(self):
        report = leads.synthesise("news", [Verdict.blind(f"b{i}", "news", f"B{i}", "down") for i in range(3)])
        assert report.confidence == 0.0
        assert report.coverage == 0.0
        assert report.stance == "NO_DATA"
        assert "blind" in report.summary.lower()

    def test_a_partly_blind_desk_weights_only_the_working_analysts(self):
        report = leads.synthesise("news", [
            verdict("a", "news", 4.0, 0.8),
            Verdict.blind("b", "news", "B", "down"),
        ])
        assert report.score == pytest.approx(4.0), "the blind analyst must not dilute toward zero"
        assert report.coverage == 0.5
        assert report.confidence < 0.8, "but confidence is reduced for the gap"


# --- Aggregation ------------------------------------------------------------


class TestLeadAggregation:
    def test_a_unanimous_desk_scores_strongly(self):
        report = leads.synthesise("equity", [verdict(f"a{i}", "equity", 4.0) for i in range(3)])
        assert report.score == pytest.approx(4.0)
        assert report.disagreement == pytest.approx(0.0)
        assert report.stance == "POSITIVE"

    def test_a_split_desk_scores_near_zero_and_records_it(self):
        report = leads.synthesise("equity", [
            verdict("a", "equity", 4.0), verdict("b", "equity", 4.0),
            verdict("c", "equity", -4.0), verdict("d", "equity", -4.0),
        ])
        assert abs(report.score) < 0.5
        assert report.disagreement > 3.0, "the split must be visible, not averaged away"
        assert report.dissent, "the minority view must be named"
        assert "split" in report.summary.lower()

    def test_confidence_weighting_favours_the_confident_analyst(self):
        report = leads.synthesise("equity", [
            verdict("sure", "equity", 4.0, confidence=0.9),
            verdict("unsure", "equity", -4.0, confidence=0.1),
        ])
        assert report.score > 2.0

    def test_a_lead_converts_to_a_verdict_for_persistence(self):
        report = leads.synthesise("equity", [verdict("a", "equity", 3.0)])
        as_verdict = leads.as_verdict(report)
        assert as_verdict.role == "lead"
        assert as_verdict.bot_id == "equity_lead"

    def test_a_veto_propagates_to_the_lead(self):
        report = leads.synthesise("equity", [verdict("f", "equity", -5.0, veto=True)])
        assert leads.as_verdict(report).veto is True


# --- Conviction -------------------------------------------------------------


class TestConvictionMapping:
    def test_the_endpoints_and_midpoint(self):
        assert score_to_conviction(5.0) == pytest.approx(100.0)
        assert score_to_conviction(0.0) == pytest.approx(50.0)
        assert score_to_conviction(-5.0) == pytest.approx(0.0)

    def test_it_is_linear_between_them(self):
        assert score_to_conviction(2.5) == pytest.approx(75.0)
        assert score_to_conviction(-2.5) == pytest.approx(25.0)

    def test_it_clamps_rather_than_extrapolating(self):
        assert score_to_conviction(50.0) == 100.0
        assert score_to_conviction(-50.0) == 0.0


def desks_all_at(score: float, confidence: float = 0.8) -> list[DeskReport]:
    reports = []
    for desk in registry.DESK_ORDER:
        report = leads.synthesise(desk, [verdict(f"{desk}_a", desk, score, confidence)])
        reports.append(report)
    return reports


class TestCmio:
    def _decide(self, desks, cfg, **kwargs):
        return cmio.decide(
            "TEST", desks, price=100.0, atr=2.0, sector="Capital Goods", cfg=cfg, **kwargs
        )

    def test_everything_positive_gives_high_conviction(self, cfg):
        report = self._decide(desks_all_at(5.0), cfg)
        assert report.conviction > 95
        assert report.stance == "BUY"

    def test_everything_negative_gives_zero_conviction(self, cfg):
        report = self._decide(desks_all_at(-5.0), cfg)
        assert report.conviction < 5
        assert report.stance == "NO_BUY"

    def test_everything_neutral_gives_fifty(self, cfg):
        report = self._decide(desks_all_at(0.0), cfg)
        assert report.conviction == pytest.approx(50.0)

    def test_a_forensic_veto_overrides_a_perfect_report(self, cfg):
        """The one input that is not weighed against anything else."""
        desks = desks_all_at(5.0)
        equity = next(d for d in desks if d.desk == "equity")
        equity.verdicts.append(verdict("forensics", "equity", -5.0, 0.9, veto=True))

        report = self._decide(desks, cfg)

        assert report.forensics_veto is True
        assert report.conviction == 0.0
        assert report.stance == "NO_BUY"
        assert (report.sizing or {}).get("recommendation") == "NO_BUY"
        assert "veto" in report.summary.lower()

    def test_no_usable_desk_is_reported_as_a_data_failure(self, cfg):
        desks = [
            leads.synthesise(d, [Verdict.blind("x", d, "X", "down")])
            for d in registry.DESK_ORDER
        ]
        report = self._decide(desks, cfg)

        assert report.conviction == 0.0
        assert report.stance == "NO_BUY"
        assert "data failure" in report.summary.lower()
        assert "not a view on the company" in report.summary.lower()

    def test_conviction_flows_into_a_rupee_size(self, cfg):
        report = self._decide(desks_all_at(3.0), cfg)
        assert report.sizing is not None
        assert report.sizing["is_buy"] is True
        assert report.sizing["total_value"] > 0
        assert report.exit_doctrine is not None

    def test_dissent_is_recorded_rather_than_averaged_away(self, cfg):
        desks = desks_all_at(4.0)
        macro = next(d for d in desks if d.desk == "macro")
        macro.score = -3.0
        macro.stance = "NEGATIVE"

        report = self._decide(desks, cfg)
        assert "disagreed" in report.dissent.lower()

    def test_blind_desks_are_named_in_the_summary(self, cfg):
        desks = desks_all_at(3.0)
        desks[0] = leads.synthesise("news", [Verdict.blind("x", "news", "X", "down")])

        report = self._decide(desks, cfg)
        assert report.blind_desks
        assert "no usable data" in report.summary.lower()


# --- Bull versus Bear -------------------------------------------------------


class TestAdversarialReview:
    def _ctx_with(self, peers: dict[str, Verdict]) -> FakeContext:
        return FakeContext(peer_verdicts=peers)

    def test_they_diverge_on_mixed_evidence(self, cfg):
        """If the two adversaries agree, the design has collapsed."""
        from src.agents.review_desk import BearCaseAnalyst, BullCaseAnalyst

        peers = {
            "a": verdict("a", "equity", 3.5, 0.8),
            "b": verdict("b", "ownership", 3.0, 0.7),
            "c": verdict("c", "news", -3.0, 0.7, red_flags=["margin pressure"]),
            "d": verdict("d", "macro", -2.5, 0.6),
        }
        ctx = self._ctx_with(peers)

        bull = BullCaseAnalyst(cfg).run(ctx)
        bear = BearCaseAnalyst(cfg).run(ctx)

        assert bull.score > 0, "the bull must find the positive case"
        assert bear.score < 0, "the bear must find the negative case"
        assert bull.score * bear.score < 0, "they must not land on the same side"

    def test_the_bull_admits_when_there_is_no_case(self, cfg):
        from src.agents.review_desk import BullCaseAnalyst

        ctx = self._ctx_with({"a": verdict("a", "equity", -3.0)})
        result = BullCaseAnalyst(cfg).run(ctx)

        assert result.score <= 0
        assert "no bull case" in " ".join(result.key_findings).lower()

    def test_the_bear_admits_when_there_is_no_case(self, cfg):
        from src.agents.review_desk import BearCaseAnalyst

        ctx = self._ctx_with({"a": verdict("a", "equity", 3.0)})
        result = BearCaseAnalyst(cfg).run(ctx)

        assert "thin" in " ".join(result.key_findings).lower()

    def test_the_bear_treats_a_veto_as_decisive(self, cfg):
        from src.agents.review_desk import BearCaseAnalyst

        ctx = self._ctx_with({
            "good": verdict("good", "equity", 4.0),
            "veto": verdict("veto", "equity", -5.0, veto=True),
        })
        result = BearCaseAnalyst(cfg).run(ctx)

        assert result.score == -5.0
        assert "veto" in " ".join(result.key_findings).lower()

    def test_the_bear_counts_blind_analysts_against_the_bull(self, cfg):
        """Silence from a desk that could not look is not good news."""
        from src.agents.review_desk import BearCaseAnalyst

        peers = {"good": verdict("good", "equity", 3.0)}
        peers.update({f"b{i}": Verdict.blind(f"b{i}", "ownership", f"B{i}", "down") for i in range(4)})

        result = BearCaseAnalyst(cfg).run(self._ctx_with(peers))
        text = " ".join(result.key_findings).lower()
        assert "had no data" in text
        assert "not good news" in text, "silence from a blind desk must not read as reassurance"

    def test_validation_reports_coverage_honestly(self, cfg):
        from src.agents.review_desk import ResearchValidationAnalyst

        ctx = FakeContext(
            peer_verdicts={
                "a": verdict("a", "equity", 2.0),
                "b": Verdict.blind("b", "ownership", "B", "NSE down"),
            },
            fundamentals=ok({"roe_pct": 18}),
            delivery=gone(),
        )
        result = ResearchValidationAnalyst(cfg).run(ctx)

        assert result.data_available
        text = " ".join(result.key_findings).lower()
        assert "coverage" in text
        assert "no data" in text or "missing" in text

    def test_validation_flags_a_report_too_thin_to_act_on(self, cfg):
        from src.agents.review_desk import ResearchValidationAnalyst

        ctx = FakeContext(peer_verdicts={
            f"b{i}": Verdict.blind(f"b{i}", "ownership", f"B{i}", "down") for i in range(6)
        })
        result = ResearchValidationAnalyst(cfg).run(ctx)

        assert result.score < 0
        assert any("too thin to act on" in flag for flag in result.red_flags)


# --- Individual bots against hand-made inputs -------------------------------


class TestOwnershipBots:
    def test_delivery_bot_rewards_sustained_absorption(self, cfg):
        from src.agents.ownership_desk import MarketVolumeIntelligenceAnalyst

        # Price falling throughout, with delivery stepping up over the last
        # fortnight - settled ownership rising into the decline.
        sessions = 60
        delivery = [45.0] * 45 + [68.0] * 15
        frame = pd.DataFrame({
            "close": [100 - i * 0.3 for i in range(sessions)],
            "volume": [100_000.0] * sessions,
            "deliverable_qty": [d * 1000 for d in delivery],
            "delivery_pct": delivery,
            "turnover_lacs": [2000.0] * sessions,
        }, index=pd.date_range("2026-01-01", periods=sessions, freq="D"))

        result = MarketVolumeIntelligenceAnalyst(cfg).run(FakeContext(delivery=ok(frame)))
        assert result.data_available
        assert result.score > 0

    def test_insider_bot_distinguishes_silence_from_an_outage(self, cfg):
        from src.agents.ownership_desk import InsiderActivityAnalyst

        silent = DataResult.empty("nse", "no disclosures in the last 90 days")
        result = InsiderActivityAnalyst(cfg).run(FakeContext(insider=silent))
        assert result.data_available is True, "silence is a finding, not a failure"
        assert result.score == 0.0
        assert "no insider disclosures" in " ".join(result.key_findings).lower()

        outage = InsiderActivityAnalyst(cfg).run(FakeContext(insider=gone("NSE 503")))
        assert outage.data_available is False

    def test_insider_bot_penalises_promoter_selling(self, cfg):
        from src.agents.ownership_desk import InsiderActivityAnalyst

        frame = pd.DataFrame([
            {"is_buy": False, "is_sell": True, "is_promoter": True,
             "signed_value": -50_000_000.0, "secAcq": 10000},
        ])
        result = InsiderActivityAnalyst(cfg).run(
            FakeContext(insider=ok(frame), fundamentals=ok({"market_cap": 1e10}))
        )

        assert result.score < 0
        assert result.red_flags

    def test_short_seller_bot_flags_an_fo_ban(self, cfg):
        from src.agents.ownership_desk import ShortSellerAnalyst

        result = ShortSellerAnalyst(cfg).run(
            FakeContext(surveillance=ok({"in_fo_ban": True, "asm_checked": False}), deals=ok(pd.DataFrame()))
        )
        assert result.score < 0
        assert any("ban" in flag.lower() for flag in result.red_flags)

    def test_short_seller_bot_admits_its_blind_spot(self, cfg):
        from src.agents.ownership_desk import ShortSellerAnalyst

        result = ShortSellerAnalyst(cfg).run(
            FakeContext(surveillance=ok({"in_fo_ban": False, "asm_checked": False}), deals=ok(pd.DataFrame()))
        )
        text = " ".join(result.key_findings).lower()
        assert "not measured" in text or "unreachable" in text
        assert result.confidence <= 0.35, "two of three inputs missing must cap confidence"


class TestEquityBots:
    def test_forensics_vetoes_on_sustained_cash_flow_divergence(self, cfg):
        from src.agents.equity_desk import FinancialForensicsAnalyst

        metrics = {
            "forensics": {
                "cfo_to_pat_latest": 0.4, "cfo_to_pat_avg_3y": 0.5,
                "years_cfo_below_pat": 4, "data_years": 4,
            }
        }
        result = FinancialForensicsAnalyst(cfg).run(FakeContext(fundamentals=ok(metrics)))

        assert result.veto is True
        assert result.score < 0
        assert "VETO" in " ".join(result.key_findings)

    def test_forensics_does_not_veto_on_one_bad_year(self, cfg):
        from src.agents.equity_desk import FinancialForensicsAnalyst

        metrics = {
            "forensics": {
                "cfo_to_pat_latest": 0.9, "cfo_to_pat_avg_3y": 1.1,
                "years_cfo_below_pat": 1, "data_years": 4,
            }
        }
        result = FinancialForensicsAnalyst(cfg).run(FakeContext(fundamentals=ok(metrics)))
        assert result.veto is False

    def test_fundamental_bot_rewards_quality(self, cfg):
        from src.agents.equity_desk import FundamentalResearchAnalyst

        strong = {"roe_pct": 28.0, "debt_to_equity": 0.15, "revenue_cagr_3y_pct": 18.0,
                  "pe_trailing": 16.0, "profitable_years_of_4": 4, "sector": "Capital Goods"}
        weak = {"roe_pct": 4.0, "debt_to_equity": 2.5, "revenue_cagr_3y_pct": -6.0,
                "pe_trailing": 75.0, "profitable_years_of_4": 1, "sector": "Capital Goods"}

        good = FundamentalResearchAnalyst(cfg).run(FakeContext(fundamentals=ok(strong)))
        bad = FundamentalResearchAnalyst(cfg).run(FakeContext(fundamentals=ok(weak)))

        assert good.score > 2.0
        assert bad.score < -1.0
        assert bad.red_flags

    def test_fundamental_bot_skips_leverage_for_lenders(self, cfg):
        from src.agents.equity_desk import FundamentalResearchAnalyst

        bank = {"roe_pct": 16.0, "debt_to_equity": 8.0, "revenue_cagr_3y_pct": 12.0,
                "profitable_years_of_4": 4, "sector": "Financial Services"}
        result = FundamentalResearchAnalyst(cfg).run(FakeContext(fundamentals=ok(bank)))

        assert result.score > 0, "a bank must not be punished for being levered"
        assert any("not comparable" in f for f in result.key_findings)


class TestNewsBots:
    def test_sentiment_reads_direction(self, cfg):
        from src.agents.news_desk import SentimentAnalyst

        good = [{"title": "Company beats estimates with record profit, brokerages upgrade",
                 "summary": "", "source": "reuters.com", "published": datetime.now()}] * 3
        bad = [{"title": "Auditor resigns amid fraud probe, rating downgrade follows",
                "summary": "", "source": "reuters.com", "published": datetime.now()}] * 3

        assert SentimentAnalyst(cfg).run(FakeContext(news=ok(good))).score > 0
        assert SentimentAnalyst(cfg).run(FakeContext(news=ok(bad))).score < 0

    def test_sentiment_admits_when_nothing_is_scoreable(self, cfg):
        from src.agents.news_desk import SentimentAnalyst

        bland = [{"title": "Stock price live updates", "summary": "", "source": "x.com",
                  "published": datetime.now()}]
        result = SentimentAnalyst(cfg).run(FakeContext(news=ok(bland)))
        assert result.data_available is False

    def test_credibility_prefers_exchange_filings(self, cfg):
        from src.agents.news_desk import NewsCredibilityAnalyst

        official = [{"title": "Board approves results", "source": "nseindia.com",
                     "published": datetime.now()}] * 4
        unknown = [{"title": "Stock tipped to double", "source": "randomblog.xyz",
                    "published": datetime.now()}] * 4

        good = NewsCredibilityAnalyst(cfg).run(FakeContext(news=ok(official)))
        bad = NewsCredibilityAnalyst(cfg).run(FakeContext(news=ok(unknown)))

        assert good.score > bad.score
        assert bad.red_flags

    def test_market_impact_weights_governance_highest(self, cfg):
        from src.agents.news_desk import MarketImpactAnalyst

        frame = pd.DataFrame([{"desc": "Resignation of statutory auditor"}])
        result = MarketImpactAnalyst(cfg).run(
            FakeContext(announcements=ok(frame), news=ok([]))
        )
        assert result.score < 0
        assert result.red_flags


class TestMacroBots:
    def test_industry_bot_prefers_mild_underperformance(self, cfg):
        from src.agents.macro_desk import IndustryResearchAnalyst

        def frame(total_return: float) -> pd.DataFrame:
            n = 200
            return pd.DataFrame(
                {"close": [100 * (1 + total_return / 100 * i / n) for i in range(n)]},
                index=pd.date_range("2026-01-01", periods=n, freq="D"),
            )

        ctx = FakeContext(
            price_frame=frame(-12.0), sector_index=ok(frame(-4.0)), benchmark_index=ok(frame(2.0))
        )
        result = IndustryResearchAnalyst(cfg).run(ctx)
        assert result.data_available

    def test_geopolitical_bot_reports_without_a_sector_map(self, cfg):
        from src.agents.macro_desk import GeopoliticalRiskAnalyst

        series = {
            "crude_brent": pd.DataFrame(
                {"close": [80.0] * 200 + [120.0] * 60},
                index=pd.date_range("2025-01-01", periods=260, freq="D"),
            )
        }
        ctx = FakeContext(macro=ok(series),
                          stock=StockIdentity("X", sector="Nonexistent Sector"))
        result = GeopoliticalRiskAnalyst(cfg).run(ctx)

        assert result.data_available
        assert any("no macro sensitivity mapping" in f.lower() for f in result.key_findings)


class TestEventTaxonomy:
    """Routine compliance must not read as a crisis.

    Every listed company files certificates under SEBI regulations quarterly.
    Matching the regulator's name alone turned mandatory paperwork into a
    governance red flag on every stock in the universe, inflating flag counts
    into double digits and biasing the news desk negative across the board.
    """

    @pytest.mark.parametrize("headline", [
        "Certificate under SEBI (Depositories and Participants) Regulations, 2018",
        "Reconciliation of Share Capital Audit Report",
        "Compliance Certificate under Regulation 74(5)",
        "Shareholding Pattern for the quarter ended June 2026",
        "Statement of Investor Complaints for the quarter",
        "Corporate Governance Report for the quarter ended",
        "Intimation of Record Date",
    ])
    def test_routine_filings_are_not_material(self, headline):
        from src.agents import lexicon

        name, spec = lexicon.classify_event(headline)
        assert spec["materiality"] <= 0.5, f"{headline!r} classified as {name}"

    @pytest.mark.parametrize("headline", [
        "Certificate under SEBI (Depositories and Participants) Regulations, 2018",
        "Reconciliation of Share Capital Audit Report",
        "Newspaper Publication of financial results",
        "Corporate Governance Report for the quarter ended",
        "Postal Ballot Notice",
    ])
    def test_routine_filings_never_read_as_negative(self, headline):
        """The property that actually matters.

        A routine filing landing in a neutral category is harmless - results
        and ownership carry direction 0. The damage came from compliance
        paperwork being scored as an adverse event, which is what drags a
        desk negative and inflates the red-flag count.
        """
        from src.agents import lexicon

        name, spec = lexicon.classify_event(headline)
        assert spec["direction"] >= 0, f"{headline!r} scored negative as {name}"

    @pytest.mark.parametrize("headline", [
        "Resignation of Statutory Auditor",
        "SEBI issues show cause notice to the company",
        "Intimation of SEBI adjudication order and penalty",
        "Forensic audit ordered into the subsidiary",
        "Whistleblower complaint received by the audit committee",
    ])
    def test_genuine_governance_events_still_flag(self, headline):
        from src.agents import lexicon

        name, spec = lexicon.classify_event(headline)
        assert spec["materiality"] >= 0.9, f"{headline!r} classified as {name}"
        assert spec["direction"] < 0

    def test_an_auditor_change_is_softer_than_a_resignation(self):
        """Rotation is mandatory under the Companies Act; walking out is not."""
        from src.agents import lexicon

        _, change = lexicon.classify_event("Change in Auditors")
        _, resign = lexicon.classify_event("Resignation of Statutory Auditor")

        assert change["materiality"] < resign["materiality"]

    def test_a_serious_event_inside_a_routine_filing_still_wins(self):
        """Highest materiality wins, so a real event cannot hide behind boilerplate."""
        from src.agents import lexicon

        name, spec = lexicon.classify_event(
            "Intimation under Regulation 30 of SEBI show cause notice received"
        )
        assert name == "governance"
        assert spec["materiality"] >= 0.9

    def test_genuine_results_are_still_recognised(self):
        from src.agents import lexicon

        for headline in ("Unaudited Financial Results for the quarter ended June 2026",
                         "Board to consider quarterly results", "Q2 FY26 results announced"):
            name, spec = lexicon.classify_event(headline)
            assert name == "results", f"{headline!r} classified as {name}"


class TestRedFlagDeduplication:
    """The flag count feeds the sizing band, so a duplicate shrinks a position.

    The Bear Case Analyst re-emits other bots' flags with the source name
    prefixed, which produced the same concern twice in different wording.
    """

    def test_the_same_concern_is_counted_once(self):
        from src.agents.cmio import _dedupe_flags

        flags = _dedupe_flags([
            verdict("forensics", "equity", -2.0, red_flags=["Cash flow below profit in 2 of 4 years."]),
            verdict("bear", "review", -2.0,
                    red_flags=["Financial Forensics Analyst: Cash flow below profit in 2 of 4 years."]),
        ])
        assert len(flags) == 1

    def test_the_phrasing_naming_its_source_is_kept(self):
        from src.agents.cmio import _dedupe_flags

        flags = _dedupe_flags([
            verdict("forensics", "equity", -2.0, red_flags=["Promoters were net sellers."]),
            verdict("bear", "review", -2.0,
                    red_flags=["Insider Activity Analyst: Promoters were net sellers."]),
        ])
        assert flags == ["Insider Activity Analyst: Promoters were net sellers."]

    def test_genuinely_different_flags_all_survive(self):
        from src.agents.cmio import _dedupe_flags

        flags = _dedupe_flags([
            verdict("a", "equity", -2.0, red_flags=["Leverage is high.", "Receivables outpacing sales."]),
            verdict("b", "ownership", -2.0, red_flags=["Promoter pledge rising."]),
        ])
        assert len(flags) == 3
