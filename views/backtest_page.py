"""Backtest - does the strategy actually beat holding the index?"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import dashboard_password, load_config
from src.strategy import backtest as bt
from src.ui import theme



cfg = load_config()

st.markdown("# Backtest")
st.caption("Walk-forward, no look-ahead. The question is whether the rules beat simply holding the index.")


with st.sidebar:
    st.markdown("### Window")
    preset = st.selectbox(
        "Period",
        ["Last 6 years", "2017-2023 (includes crashes)", "2018-2021 (COVID)", "Custom"],
        index=1,
        help="2017-2023 spans the 2018-19 correction and the COVID crash, so it is a "
             "fairer test than a window that is only the post-COVID bull run.",
    )

    today = date.today()
    if preset == "Last 6 years":
        start, end = today - timedelta(days=int(365.25 * 6)), today
    elif preset.startswith("2017"):
        start, end = date(2017, 1, 1), date(2023, 1, 1)
    elif preset.startswith("2018"):
        start, end = date(2018, 1, 1), date(2021, 6, 30)
    else:
        start = st.date_input("From", date(2019, 1, 1))
        end = st.date_input("To", today)

    universe_limit = st.slider("Universe size", 50, 500, 200, step=50)
    capital = st.number_input("Capital (Rs)", 100_000, 100_000_000, 1_000_000, step=100_000)
    max_positions = st.slider("Max positions", 5, 25, 15)
    ab_test = st.checkbox(
        "A/B the trend filter", value=False,
        help="Runs the same rules with and without the 200-DMA condition.",
    )

    run = st.button("Run backtest", type="primary", use_container_width=True)


st.markdown(
    '<div class="disclaimer"><strong>Read the caveats before trusting a number here.</strong> '
    "The universe is today's index membership, so companies that dropped out are missing and "
    "returns are flattered. Stage 3 delivery confirmation is not applied, because it needs one "
    "NSE request per session; since it only ever removes candidates, these figures are a floor. "
    "Past performance is not a forecast.</div>",
    unsafe_allow_html=True,
)


if run:
    bar = st.progress(0.0, text="Loading price history...")

    def report(done: int, total: int, label: str) -> None:
        bar.progress(min(done / max(total, 1), 1.0), text=f"{label}  ({done}/{total} sessions)")

    try:
        with st.spinner("Walking forward..."):
            result = bt.run_backtest(
                start=start, end=end, cfg=cfg,
                initial_capital=float(capital),
                max_positions=max_positions,
                universe_limit=universe_limit,
                progress=report,
            )
        bar.empty()
        st.session_state["backtest"] = result
        st.session_state["backtest_ab"] = None

        if ab_test:
            with st.spinner("Running the control arm without the trend filter..."):
                st.session_state["backtest_ab"] = bt.compare_trend_filter(
                    cfg=cfg, start=start, end=end,
                    initial_capital=float(capital),
                    max_positions=max_positions,
                    universe_limit=universe_limit,
                )
    except Exception as exc:
        bar.empty()
        st.error(f"Backtest failed: {exc}")


result = st.session_state.get("backtest")

if result is None:
    st.info("Choose a window in the sidebar and run a backtest.")
    st.stop()

stats = result.stats()

if stats.get("trades", 0) == 0:
    st.warning("No trades were generated in this window.")
    st.stop()


# --- Headline ---------------------------------------------------------------

strategy_cagr = stats.get("strategy_cagr_pct")
benchmark_cagr = stats.get("benchmark_cagr_pct")
excess = stats.get("excess_cagr_pct")

st.markdown("## Result")

if excess is not None:
    verdict = (
        "The strategy beat the index over this window."
        if excess > 0 else
        "The strategy underperformed the index over this window."
    )
    colour = theme.COLORS["positive"] if excess > 0 else theme.COLORS["negative"]
    drawdown_better = (
        stats.get("strategy_max_drawdown_pct", -100) > stats.get("benchmark_max_drawdown_pct", -100)
    )
    st.markdown(
        f'<div class="card"><div style="font-size:1.05rem;color:{colour}">{verdict}</div>'
        f'<div style="color:#94A3B8;font-size:0.88rem;margin-top:0.3rem">'
        f'{theme.pct(strategy_cagr)} CAGR against {theme.pct(benchmark_cagr)} for the index, '
        f'a difference of {theme.pct(excess, 2, True)}. Maximum drawdown was '
        f'{theme.pct(stats.get("strategy_max_drawdown_pct"))} against '
        f'{theme.pct(stats.get("benchmark_max_drawdown_pct"))} '
        f'- {"shallower" if drawdown_better else "deeper"} than simply holding.'
        f"</div></div>",
        unsafe_allow_html=True,
    )

row1 = st.columns(5)
row1[0].metric("CAGR", theme.pct(strategy_cagr), theme.pct(excess, 2, True) if excess else None)
row1[1].metric("Sharpe", theme.num(stats.get("strategy_sharpe"), 2))
row1[2].metric("Max drawdown", theme.pct(stats.get("strategy_max_drawdown_pct")))
row1[3].metric("Volatility", theme.pct(stats.get("strategy_volatility_pct")))
row1[4].metric("Sortino", theme.num(stats.get("strategy_sortino"), 2))

row2 = st.columns(5)
row2[0].metric("Trades", stats.get("trades"))
row2[1].metric("Win rate", theme.pct(stats.get("win_rate_pct")))
row2[2].metric("Payoff ratio", theme.num(stats.get("payoff_ratio"), 2))
row2[3].metric("Avg holding", f"{stats.get('avg_holding_days')} d")
row2[4].metric("Expectancy", theme.pct(stats.get("expectancy_pct")))


# --- Equity curve -----------------------------------------------------------

if result.equity_curve is not None:
    st.markdown("## Equity curve")
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=result.equity_curve.index, y=result.equity_curve.values,
            name="Strategy", line=dict(color=theme.COLORS["accent"], width=2),
        )
    )
    if result.benchmark_curve is not None:
        fig.add_trace(
            go.Scatter(
                x=result.benchmark_curve.index, y=result.benchmark_curve.values,
                name="Nifty 500 buy and hold",
                line=dict(color="#64748B", width=1.6, dash="dash"),
            )
        )
    fig.update_layout(
        height=420, template="plotly_dark",
        paper_bgcolor=theme.COLORS["bg"], plot_bgcolor=theme.COLORS["bg"],
        font=dict(family="Fira Code, monospace", size=11, color=theme.COLORS["fg_muted"]),
        legend=dict(orientation="h", y=1.08, x=0, bgcolor="rgba(0,0,0,0)"),
        margin=dict(l=8, r=8, t=8, b=8), hovermode="x unified",
        yaxis_title="Portfolio value (Rs)",
    )
    fig.update_xaxes(gridcolor="#1A1E2F")
    fig.update_yaxes(gridcolor="#1A1E2F")
    st.plotly_chart(fig, use_container_width=True)


# --- The money-left-on-the-table number -------------------------------------

captured = stats.get("avg_captured_fraction")
if captured is not None:
    st.markdown("## How much was left on the table")
    cols = st.columns(3)
    cols[0].markdown(
        theme.stat("Avg peak gain reached", theme.pct(stats.get("avg_mfe_pct")),
                   "best price while held"),
        unsafe_allow_html=True,
    )
    cols[1].markdown(
        theme.stat("Avg realised return", theme.pct(stats.get("avg_return_pct"))),
        unsafe_allow_html=True,
    )
    cols[2].markdown(
        theme.stat("Captured", f"{captured:.0%}",
                   "share of the available move actually taken"),
        unsafe_allow_html=True,
    )
    st.caption(
        f"Winners captured {captured:.0%} of their peak gain, so roughly "
        f"{1 - captured:.0%} of the available move was given back before exit. This is the "
        f"exact number the self-learning bot will attack when it asks how a win could have "
        f"been bigger."
    )


# --- Exits ------------------------------------------------------------------

if stats.get("exit_reasons"):
    st.markdown("## What closed the positions")
    st.dataframe(
        pd.DataFrame(
            [
                {"Exit reason": k.replace("_", " ").title(),
                 "Trades": v["count"], "Avg return %": v["avg_return_pct"]}
                for k, v in stats["exit_reasons"].items()
            ]
        ),
        use_container_width=True, hide_index=True,
        column_config={"Avg return %": st.column_config.NumberColumn(format="%.1f")},
    )


# --- A/B --------------------------------------------------------------------

ab = st.session_state.get("backtest_ab")
if ab:
    st.markdown("## Does the trend filter earn its place?")
    st.caption(
        "The strategy's central claim is that a dip is only worth buying inside an uptrend. "
        "This runs identical rules with and without the 200-DMA condition."
    )

    with_filter, without_filter = ab["with_trend_filter"], ab["without_trend_filter"]
    comparison = pd.DataFrame(
        [
            {"Metric": label,
             "With filter": with_filter.get(key),
             "Without filter": without_filter.get(key)}
            for label, key in [
                ("Trades", "trades"), ("Win rate %", "win_rate_pct"),
                ("CAGR %", "strategy_cagr_pct"), ("Sharpe", "strategy_sharpe"),
                ("Max drawdown %", "strategy_max_drawdown_pct"),
                ("Volatility %", "strategy_volatility_pct"),
            ]
        ]
    )
    st.dataframe(comparison, use_container_width=True, hide_index=True)
    st.info(f"**Verdict:** {ab['verdict']}")


# --- Survivorship -----------------------------------------------------------

st.markdown("## Survivorship adjustment")
drag = bt.estimate_survivorship_drag(result)
if "reported_cagr_pct" in drag:
    cols = st.columns(3)
    cols[0].markdown(theme.stat("Reported CAGR", theme.pct(drag["reported_cagr_pct"])), unsafe_allow_html=True)
    cols[1].markdown(
        theme.stat("After haircut", theme.pct(drag["survivorship_adjusted_cagr_pct"]),
                   f"assumes {drag['assumed_haircut_pct']:.0f}pp of bias"),
        unsafe_allow_html=True,
    )
    cols[2].markdown(
        theme.stat("Still beats index?",
                   "Yes" if drag.get("still_beats_benchmark") else "No"),
        unsafe_allow_html=True,
    )
    st.caption(drag["note"])


if result.warnings:
    with st.expander("Warnings from this run"):
        st.markdown(theme.reasons(result.warnings, "warn"), unsafe_allow_html=True)


with st.expander("Every trade"):
    st.dataframe(
        pd.DataFrame([t.to_dict() for t in result.trades]),
        use_container_width=True, hide_index=True,
    )
