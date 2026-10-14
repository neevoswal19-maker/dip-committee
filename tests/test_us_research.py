"""US research: tickers, Groww's US costs, the index lists, the volume stand-in
for delivery, and the pre-registered gate."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from src import us_charges
from src.config import load_config
from src.data import us_universe
from src.data.provider import StockIdentity
from src.strategy import research, us_research
from src.strategy.exit_policies import long_term_plans
from src.strategy.simulator import Bars

CFG = load_config()


# --- Tickers --------------------------------------------------------------------------


def test_us_tickers_go_to_yahoo_bare_with_share_classes_dashed():
    assert StockIdentity("AAPL", market="US").yahoo == "AAPL"
    assert StockIdentity("BRK.B", market="US").yahoo == "BRK-B"
    assert StockIdentity("RELIANCE").yahoo == "RELIANCE.NS"


# --- Costs ----------------------------------------------------------------------------


def test_a_buy_pays_brokerage_gst_and_conversion():
    c = us_charges.estimate("BUY", 10, 100.0, cfg=CFG, fx_pct=0.5)      # $1,000 order
    assert c.brokerage == pytest.approx(1.0)
    assert c.gst == pytest.approx(0.18)
    assert c.other == pytest.approx(5.0)
    assert c.exchange_fee == pytest.approx(10 * 0.000003)


def test_small_orders_pay_the_one_dollar_minimum():
    assert us_charges.estimate("BUY", 1, 200.0, cfg=CFG, fx_pct=0).brokerage == pytest.approx(1.0)


def test_sells_add_the_sec_and_finra_fees():
    buy = us_charges.estimate("BUY", 100, 50.0, cfg=CFG, fx_pct=0)
    sell = us_charges.estimate("SELL", 100, 50.0, cfg=CFG, fx_pct=0)
    sec = 5000 * 0.00206 / 100
    finra = 100 * 0.000195
    assert sell.exchange_fee - buy.exchange_fee == pytest.approx(sec + finra)


def test_finra_is_capped():
    sell = us_charges.estimate("SELL", 1_000_000, 1.0, cfg=CFG, fx_pct=0)
    assert sell.exchange_fee < 1_000_000 * 0.00206 / 100 + 9.79 + 4


def test_the_simulator_charges_the_us_schedule_when_given_it():
    index = pd.bdate_range("2020-01-01", periods=60)
    close = np.linspace(100, 130, 60)
    frame = pd.DataFrame({"open": close, "high": close + 1, "low": close - 1, "close": close,
                          "atr": 2.0, "atr22": 2.0}, index=index)
    bars = {"AAA": Bars.from_frame("AAA", frame, extra=("atr", "atr22"))}
    signals = {"AAA": np.zeros(60, dtype=bool)}
    signals["AAA"][5] = True
    ranks = {"AAA": np.zeros(60)}
    seen = []

    def cost(side, qty, price):
        seen.append(side)
        return 1.0

    result = research.run_long_term(long_term_plans(CFG)["L2"], bars, signals, ranks,
                                    start=date(2020, 1, 1), end=date(2020, 3, 31), cfg=CFG,
                                    capital=10_000, position_pct=50, max_positions=1, cost_fn=cost)
    assert "BUY" in seen
    assert result.trades and result.trades[0].charges >= 1.0


# --- Index lists ----------------------------------------------------------------------

SP500_HTML = """
<table id="constituents"><tr><th>Symbol</th><th>Security</th><th>GICS Sector</th><th>GICS Sub-Industry</th></tr>
<tr><td>AAPL</td><td>Apple Inc.</td><td>Information Technology</td><td>Technology Hardware</td></tr>
<tr><td>BRK.B</td><td>Berkshire Hathaway</td><td>Financials</td><td>Multi-Sector Holdings</td></tr>
</table>"""

NDX_HTML = """
<table id="constituents"><tr><th>Ticker</th><th>Company</th><th>ICB Industry [1]</th></tr>
<tr><td>AAPL</td><td>Apple Inc.</td><td>Technology</td></tr>
<tr><td>TEAM</td><td>Atlassian</td><td>Technology</td></tr>
</table>"""


def test_constituent_tables_parse_into_us_identities():
    sp = us_universe.parse_constituents(SP500_HTML)
    assert [s.symbol for s in sp] == ["AAPL", "BRK.B"]
    assert sp[1].sector == "Financials" and sp[1].market == "US"
    assert sp[1].yahoo == "BRK-B"
    ndx = us_universe.parse_constituents(NDX_HTML)
    assert [s.symbol for s in ndx] == ["AAPL", "TEAM"]
    assert ndx[1].name == "Atlassian"
    assert ndx[1].sector == "Technology"      # ICB column, footnote marker stripped


def test_the_union_drops_duplicates(monkeypatch):
    from src.data.provider import DataResult

    lists = {"sp500": us_universe.parse_constituents(SP500_HTML),
             "nasdaq100": us_universe.parse_constituents(NDX_HTML)}
    monkeypatch.setattr(us_universe, "get_index", lambda which: DataResult(value=lists[which]))
    assert [s.symbol for s in us_universe.stocks()] == ["AAPL", "BRK.B", "TEAM"]


def test_etfs_come_from_config():
    etfs = us_universe.etfs(CFG)
    assert len(etfs) == 30
    assert {"SPY", "QQQ", "GLD"} <= {e.symbol for e in etfs}
    assert all(e.market == "US" for e in etfs)


# --- The delivery stand-in ------------------------------------------------------------


def _tape(n=80, heavy_down_from=None):
    index = pd.bdate_range("2024-01-01", periods=n)
    close = pd.Series(100.0, index=index)
    close[1::2] = 99.0                                   # alternating down days
    volume = pd.Series(1_000_000.0, index=index)
    if heavy_down_from is not None:
        down = close.diff() < 0
        volume[(np.arange(n) >= heavy_down_from) & down.to_numpy()] = 1_600_000.0
    return pd.DataFrame({"close": close, "volume": volume})


def test_flat_volume_does_not_confirm():
    assert not us_research.down_volume_confirmed(_tape()).any()


def test_heavy_volume_on_down_days_confirms():
    mask = us_research.down_volume_confirmed(_tape(heavy_down_from=50))
    assert not mask.iloc[:50].any()
    assert mask.iloc[60:].all()


def test_the_stand_in_never_looks_ahead():
    tape = _tape(heavy_down_from=50)
    past = us_research.down_volume_confirmed(tape.iloc[:55])
    full = us_research.down_volume_confirmed(tape)
    assert (past == full.iloc[:55]).all()


# --- The gate -------------------------------------------------------------------------


def _outcome(cagr, dd, bench_cagr, bench_dd, trades):
    o = us_research.Outcome("x", "x")
    for name in ("2014-21", "2022+"):
        o.periods[name] = {"curve": {"cagr_pct": cagr, "max_drawdown_pct": dd},
                           "benchmark": {"cagr_pct": bench_cagr, "max_drawdown_pct": bench_dd},
                           "trades": {"trades": trades}}
    return o


def test_beating_spy_on_growth_and_drawdown_passes():
    assert us_research.judge(_outcome(15, -18, 12, -24, 80), 50).passed


@pytest.mark.parametrize("cagr,dd,trades,why", [
    (11, -18, 80, "not above SPY"),
    (15, -30, 80, "deeper than SPY"),
    (15, -18, 20, "only 20 closed trades"),
])
def test_any_shortfall_fails(cagr, dd, trades, why):
    o = us_research.judge(_outcome(cagr, dd, 12, -24, trades), 50)
    assert not o.passed
    assert any(why in r for r in o.reasons)


def test_recommend_picks_the_faster_passing_version():
    a = us_research.judge(_outcome(15, -18, 12, -24, 80), 50)
    b = us_research.judge(_outcome(17, -20, 12, -24, 80), 50)
    a.key, b.key = "dip", "dip_volume"
    assert us_research.recommend([a, b])[0] == "dip_volume"
    fail = us_research.judge(_outcome(5, -18, 12, -24, 80), 50)
    assert us_research.recommend([fail])[0] is None
