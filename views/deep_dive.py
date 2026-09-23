"""Deep Dive - everything known about one stock, and what it would be sized at."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import indicators as ind
from src import scan, screener
from src.config import dashboard_password, load_config
from src.data import fundamentals as fnd
from src.data import nse, prices
from src.data.provider import StockIdentity
from src.strategy import exit as ex
from src.strategy import sizing as sz
from src.ui import theme



cfg = load_config()

st.markdown("# Deep Dive")
st.caption("Every metric behind a single name, and the position it would justify.")


# --- Symbol selection -------------------------------------------------------

_, latest_candidates = scan.latest_candidates(include_near_misses=True)
suggested = [c["symbol"] for c in latest_candidates]

with st.sidebar:
    st.markdown("### Stock")
    if suggested:
        picked = st.selectbox("From the latest scan", ["(type my own)"] + suggested)
    else:
        picked = "(type my own)"

    typed = st.text_input(
        "Symbol", value="" if picked != "(type my own)" else "AIAENG",
        help="NSE trading symbol, for example RELIANCE or INFY",
    ).strip().upper()

    symbol = typed if picked == "(type my own)" else picked
    years = st.slider("Years of history", 1, 6, 3)


if not symbol:
    st.info("Enter a symbol in the sidebar.")
    st.stop()


@st.cache_data(ttl=1800, show_spinner=False)
def load(symbol: str, years: int):
    """Fetch and compute everything for one symbol.

    Cached for half an hour: Streamlit re-runs this script on every widget
    change, and refetching six years of history each time a slider moved
    would be both slow and a good way to get rate-limited.
    """
    stock = StockIdentity(symbol)

    history = prices.get_price_history(stock, years=years)
    frame = None
    if history.usable and history.value is not None and not history.value.empty:
        frame = ind.compute_indicator_frame(
            history.value,
            rsi_period=int(cfg.get("dip.rsi_period", 14)),
            atr_period=int(cfg.get("dip.atr_period", 14)),
            dma_long=int(cfg.get("dip.dma_long", 200)),
            dma_short=int(cfg.get("dip.dma_short", 50)),
            slope_lookback=int(cfg.get("dip.dma_slope_lookback_days", 126)),
        )

    metrics = fnd.get_fundamentals(stock)
    delivery = nse.get_delivery_history(stock, days=60)
    insider = nse.get_insider_trades(stock, days=180)
    surveillance = nse.get_surveillance_flags(stock)

    return frame, history, metrics, delivery, insider, surveillance


with st.spinner(f"Loading {symbol}..."):
    frame, history, metrics_result, delivery, insider, surveillance = load(symbol, years)

if frame is None or frame.empty:
    st.error(f"No price history for {symbol}. Check the symbol, or it may be delisted.")
    st.stop()

last = frame.iloc[-1]
close = float(last["close"])
metrics = metrics_result.value or {}


# --- Header -----------------------------------------------------------------

name = metrics.get("name") or symbol
sector = metrics.get("sector") or "--"

head = st.columns([2, 1, 1, 1, 1])
head[0].markdown(f"## {symbol}\n<span class='label'>{name} &middot; {sector}</span>", unsafe_allow_html=True)
head[1].metric("Price", theme.num(close, 2))
head[2].metric("Off 52w high", theme.pct(last.get("drawdown_pct")))
head[3].metric("RSI (14)", theme.num(last.get("rsi"), 1))
head[4].metric("ATR", f"{theme.num(last.get('atr'), 2)} ({theme.pct(last.get('atr_pct'))})")


# --- Screen verdict ---------------------------------------------------------

stock = StockIdentity(symbol, name=name, sector=sector)
dip_stage, dip_metrics = screener.evaluate_dip(frame, cfg)
delivery_stage, delivery_metrics = screener.evaluate_delivery(delivery.value, cfg)
quality_passed, quality_reasons = (
    fnd.passes_quality_gate(metrics, cfg) if metrics else (False, ["no fundamentals"])
)

st.markdown("## Screen")
stage_cols = st.columns(3)

for col, (label, passed, reasons_list, notes) in zip(
    stage_cols,
    [
        ("Quality gate", quality_passed, quality_reasons, []),
        ("Dip detection", dip_stage.passed, dip_stage.reasons, dip_stage.notes),
        ("Delivery", delivery_stage.passed, delivery_stage.reasons, delivery_stage.notes),
    ],
):
    with col:
        verdict = "PASS" if passed else "FAIL"
        colour = theme.COLORS["positive"] if passed else theme.COLORS["negative"]
        st.markdown(
            f'<div class="card"><div class="label">{label}</div>'
            f'<div style="margin:0.35rem 0">{theme.pill(verdict, colour)}</div>'
            f'{theme.reasons(reasons_list, "fail") if reasons_list else ""}'
            f'{theme.reasons(notes, "warn") if notes else ""}</div>',
            unsafe_allow_html=True,
        )


# --- Chart ------------------------------------------------------------------

st.markdown("## Price, trend and delivery")

plot = frame.tail(years * 252 if years * 252 < len(frame) else len(frame))

fig = make_subplots(
    rows=3, cols=1, shared_xaxes=True,
    row_heights=[0.56, 0.22, 0.22], vertical_spacing=0.04,
    subplot_titles=("", "RSI (14)", "Delivery %"),
)

fig.add_trace(
    go.Candlestick(
        x=plot.index, open=plot["open"], high=plot["high"], low=plot["low"], close=plot["close"],
        name=symbol,
        increasing_line_color=theme.COLORS["positive"],
        decreasing_line_color=theme.COLORS["negative"],
        increasing_fillcolor=theme.COLORS["positive"],
        decreasing_fillcolor=theme.COLORS["negative"],
    ),
    row=1, col=1,
)

for column, colour, label, width in (
    ("dma_200", "#38BDF8", "200 DMA", 1.8),
    ("dma_50", "#A78BFA", "50 DMA", 1.2),
):
    if column in plot.columns:
        fig.add_trace(
            go.Scatter(x=plot.index, y=plot[column], name=label,
                       line=dict(color=colour, width=width)),
            row=1, col=1,
        )

if "high_52w" in plot.columns:
    fig.add_trace(
        go.Scatter(x=plot.index, y=plot["high_52w"], name="52w high",
                   line=dict(color="#475569", width=1, dash="dot")),
        row=1, col=1,
    )

if "rsi" in plot.columns:
    fig.add_trace(
        go.Scatter(x=plot.index, y=plot["rsi"], name="RSI",
                   line=dict(color="#F59E0B", width=1.4)),
        row=2, col=1,
    )
    rsi_max = float(cfg.get("dip.rsi_max", 40))
    fig.add_hline(y=rsi_max, line=dict(color=theme.COLORS["positive"], width=1, dash="dash"),
                  row=2, col=1, annotation_text=f"buy zone below {rsi_max:.0f}",
                  annotation_font_size=10, annotation_font_color="#94A3B8")
    fig.add_hline(y=70, line=dict(color="#475569", width=1, dash="dot"), row=2, col=1)

if delivery.usable and delivery.value is not None and not delivery.value.empty:
    d = delivery.value
    fig.add_trace(
        go.Bar(x=d.index, y=d["delivery_pct"], name="Delivery %",
               marker_color="#22C55E", opacity=0.55),
        row=3, col=1,
    )
    rolling = d["delivery_pct"].rolling(20, min_periods=5).mean()
    fig.add_trace(
        go.Scatter(x=d.index, y=rolling, name="20d avg delivery",
                   line=dict(color="#F8FAFC", width=1.4)),
        row=3, col=1,
    )

fig.update_layout(
    height=760,
    template="plotly_dark",
    paper_bgcolor=theme.COLORS["bg"],
    plot_bgcolor=theme.COLORS["bg"],
    font=dict(family="Fira Code, monospace", size=11, color=theme.COLORS["fg_muted"]),
    xaxis_rangeslider_visible=False,
    legend=dict(orientation="h", y=1.04, x=0, bgcolor="rgba(0,0,0,0)"),
    margin=dict(l=8, r=8, t=28, b=8),
    hovermode="x unified",
)
fig.update_xaxes(gridcolor="#1A1E2F", showgrid=True)
fig.update_yaxes(gridcolor="#1A1E2F", showgrid=True)

st.plotly_chart(fig, use_container_width=True)

delivery_rows = 0 if delivery.value is None else len(delivery.value)
st.caption(
    f"The delivery panel covers the last {delivery_rows} sessions, not the full price window. "
    f"NSE publishes delivery one bhavcopy file per day, so history accumulates as the daily "
    f"scan runs rather than arriving all at once."
)


# --- Tabs -------------------------------------------------------------------

tab_size, tab_fundamentals, tab_forensics, tab_ownership = st.tabs(
    ["Position sizing", "Fundamentals", "Forensics", "Ownership"]
)


with tab_size:
    st.caption(
        "Until the committee is wired in, drive this with the conviction slider to see "
        "how the recommendation and the rupee size respond."
    )

    controls = st.columns(4)
    capital = controls[0].number_input(
        "Capital (Rs)", min_value=10_000, max_value=100_000_000,
        value=int(cfg.get("sizing.default_capital", 500_000)), step=50_000,
    )
    conviction = controls[1].slider("Conviction", 0, 100, 72)
    red_flags = controls[2].checkbox("Red flags present", value=False)
    veto = controls[3].checkbox("Forensic veto", value=False)

    decision = sz.size_position(
        symbol=symbol,
        conviction=float(conviction),
        price=close,
        atr=float(last["atr"]) if pd.notna(last.get("atr")) else 0.0,
        cfg=cfg,
        portfolio=sz.PortfolioState(capital=float(capital)),
        sector=sector,
        has_red_flags=red_flags,
        forensics_veto=veto,
    )

    colour = theme.STANCE_COLORS.get(decision.recommendation, theme.COLORS["neutral"])
    st.markdown(
        f'<div class="card"><div style="margin-bottom:0.5rem">'
        f'{theme.pill(decision.recommendation, colour)}</div>'
        f'<div style="font-size:0.92rem;line-height:1.6">{decision.narrative}</div></div>',
        unsafe_allow_html=True,
    )

    if decision.is_buy:
        summary = st.columns(4)
        summary[0].markdown(theme.stat("Total", theme.rupees(decision.total_value)), unsafe_allow_html=True)
        summary[1].markdown(theme.stat("Shares", f"{decision.total_shares:,}"), unsafe_allow_html=True)
        summary[2].markdown(
            theme.stat("Stop", theme.num(decision.stop_price, 2),
                       f"risking {theme.rupees(decision.risk_amount)}"),
            unsafe_allow_html=True,
        )
        summary[3].markdown(
            theme.stat("Of capital", theme.pct(decision.pct_of_capital),
                       f"{theme.pct(decision.risk_pct_of_capital, 2)} at risk"),
            unsafe_allow_html=True,
        )

        st.markdown("#### Staged entry")
        st.caption("Dips deepen. Committing everything at the first signal is how this strategy loses.")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Tranche": t.sequence,
                        "Share": f"{t.pct_of_position:.0f}%",
                        "Value": theme.rupees(t.value),
                        "Shares": t.shares,
                        "Trigger": t.description,
                    }
                    for t in decision.tranches
                ]
            ),
            use_container_width=True, hide_index=True,
        )

        st.markdown("#### Exit doctrine")
        doctrine = ex.build_doctrine(symbol, close, cfg, stop_price=decision.stop_price)
        st.code(doctrine.describe(), language=None)
    else:
        st.markdown(theme.reasons(decision.rejections, "fail"), unsafe_allow_html=True)

    if decision.adjustments:
        st.markdown("**Adjustments**")
        st.markdown(theme.reasons(decision.adjustments, "warn"), unsafe_allow_html=True)


with tab_fundamentals:
    if not metrics:
        st.warning(f"No fundamentals available. {metrics_result.note or ''}")
    else:
        st.caption(f"Source: {metrics_result.describe()}")
        grid = st.columns(4)
        fields = [
            ("Market cap", theme.crores(metrics.get("market_cap_cr"))),
            ("P/E (trailing)", theme.num(metrics.get("pe_trailing"), 1)),
            ("P/B", theme.num(metrics.get("price_to_book"), 2)),
            ("ROE", theme.pct(metrics.get("roe_pct"))),
            ("Debt / equity", theme.num(metrics.get("debt_to_equity"), 2)),
            ("Revenue CAGR 3y", theme.pct(metrics.get("revenue_cagr_3y_pct"))),
            ("Profit CAGR 3y", theme.pct(metrics.get("profit_cagr_3y_pct"))),
            ("Net margin", theme.pct(metrics.get("profit_margin_pct"))),
            ("Operating margin", theme.pct(metrics.get("operating_margin_pct"))),
            ("Profitable years", f"{metrics.get('profitable_years_of_4', '--')} of 4"),
            ("Beta", theme.num(metrics.get("beta"), 2)),
            ("Statement years", str(metrics.get("statement_years", "--"))),
        ]
        for i, (label, value) in enumerate(fields):
            grid[i % 4].markdown(theme.stat(label, value), unsafe_allow_html=True)


with tab_forensics:
    forensics = (metrics or {}).get("forensics", {})
    if not forensics:
        st.warning("No statements available to run forensics against.")
    else:
        st.caption(
            "Plain ratios, not a blended score. The point is that a bot - and you - can "
            "reason about each one; a single elevated figure is ordinary, three pointing "
            "the same way is a pattern."
        )

        cfo = forensics.get("cfo_to_pat_latest")
        years_below = forensics.get("years_cfo_below_pat")
        grid = st.columns(3)
        grid[0].markdown(
            theme.stat("CFO / PAT (latest)", theme.num(cfo, 2),
                       "below 1.0 means profit is not becoming cash"),
            unsafe_allow_html=True,
        )
        grid[1].markdown(
            theme.stat("CFO / PAT (3y avg)", theme.num(forensics.get("cfo_to_pat_avg_3y"), 2)),
            unsafe_allow_html=True,
        )
        grid[2].markdown(
            theme.stat("Years CFO below PAT", f"{years_below if years_below is not None else '--'} of 4",
                       "a run of years is the signal"),
            unsafe_allow_html=True,
        )

        grid2 = st.columns(3)
        grid2[0].markdown(
            theme.stat("Receivables vs sales", theme.pct(forensics.get("receivables_vs_sales_gap_pct"), 1, True),
                       "receivables growing faster than sales"),
            unsafe_allow_html=True,
        )
        grid2[1].markdown(
            theme.stat("Receivable days", theme.num(forensics.get("receivable_days"), 0)),
            unsafe_allow_html=True,
        )
        grid2[2].markdown(
            theme.stat("Debt growth YoY", theme.pct(forensics.get("debt_growth_yoy_pct"), 1, True)),
            unsafe_allow_html=True,
        )

        if forensics.get("net_margin_trend_pct"):
            st.markdown("**Net margin, newest first**")
            st.markdown(
                f'<span class="mono">{forensics["net_margin_trend_pct"]}</span> '
                f'&mdash; {forensics.get("margin_direction", "")}',
                unsafe_allow_html=True,
            )


with tab_ownership:
    left, right = st.columns(2)

    with left:
        st.markdown("#### Insider activity")
        st.caption(f"{insider.describe()}")
        if insider.ok and insider.value is not None and not insider.value.empty:
            trades = insider.value
            net_value = trades["signed_value"].sum(skipna=True) if "signed_value" in trades else 0
            buys = int(trades["is_buy"].sum()) if "is_buy" in trades else 0
            sells = int(trades["is_sell"].sum()) if "is_sell" in trades else 0

            st.markdown(
                theme.stat(
                    "Net insider flow",
                    theme.rupees(net_value),
                    f"{buys} buys, {sells} sells in 180 days",
                ),
                unsafe_allow_html=True,
            )
            display = [c for c in ["disclosed_at", "acqName", "personCategory",
                                   "tdpTransactionType", "secAcq", "secVal", "acqMode"]
                       if c in trades.columns]
            st.dataframe(trades[display].head(20), use_container_width=True, hide_index=True)
        else:
            st.info(insider.note or "No insider disclosures.")

    with right:
        st.markdown("#### Surveillance")
        if surveillance.value:
            flags = surveillance.value
            in_ban = flags.get("in_fo_ban")
            st.markdown(
                theme.stat(
                    "F&O ban",
                    "YES" if in_ban else "No",
                    "banned stocks signal stressed positioning" if in_ban else "not in today's ban list",
                ),
                unsafe_allow_html=True,
            )
            if not flags.get("asm_checked"):
                st.markdown(
                    theme.reasons(
                        ["NSE's ASM and GSM surveillance lists are unreachable from here, "
                         "so this is not a complete check."],
                        "warn",
                    ),
                    unsafe_allow_html=True,
                )
        else:
            st.info("No surveillance data.")
