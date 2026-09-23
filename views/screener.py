"""Screener - run a scan and inspect what survived, and what did not."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import scan
from src.config import is_deployed, load_config
from src.ui import theme



cfg = load_config()

st.markdown("# Screener")
st.caption(
    "Three stages, cheapest first. Most of the universe is rejected on price data alone "
    "before anything expensive is fetched."
)


# --- Controls ---------------------------------------------------------------

with st.sidebar:
    st.markdown("### Run a scan")
    universe_choice = st.selectbox(
        "Universe",
        ["NIFTY 500", "NIFTY 200", "NIFTY 100", "NIFTY 50"],
        index=0,
    )
    quick = st.checkbox(
        "Quick scan (first 100 only)",
        value=False,
        help="Screens a slice of the universe. Useful for a fast look; not a complete scan.",
    )
    skip_quality = st.checkbox(
        "Skip the fundamentals gate",
        value=False,
        help="Faster, because per-stock fundamentals are the slow part. "
             "Results then reflect price and delivery only.",
    )

    # A full scan is ~3.5 minutes and several hundred megabytes of price
    # frames. On a 1 GB Streamlit Cloud instance that is slow at best and
    # killed at worst, and every visitor could trigger one. In deployment the
    # scheduled job owns scanning and the dashboard only reads.
    deployed = is_deployed()
    if deployed:
        st.info(
            "Scanning runs as a scheduled job, not in the app - a full universe scan "
            "is too heavy for the hosted instance. Trigger it from the repository's "
            "**Actions** tab (Daily scan workflow), or wait for the 19:00 IST run."
        )

    if st.button("Run scan", type="primary", use_container_width=True, disabled=deployed):
        bar = st.progress(0.0, text="Starting...")

        def report(done: int, total: int, symbol: str) -> None:
            bar.progress(min(done / max(total, 1), 1.0), text=f"{symbol}  ({done}/{total})")

        try:
            with st.spinner("Screening..."):
                summary = scan.run_scan(
                    index=universe_choice,
                    limit=100 if quick else None,
                    check_quality=not skip_quality,
                    progress=report,
                    cfg=cfg,
                )
            bar.empty()
            st.success(
                f"Found {summary.candidates} candidates from {summary.universe_size} stocks "
                f"in {summary.duration_seconds:.0f}s."
            )
            st.rerun()
        except Exception as exc:
            bar.empty()
            st.error(f"Scan failed: {exc}")

    st.divider()
    show_near_misses = st.checkbox("Show near misses", value=True)


# --- Results ----------------------------------------------------------------

latest = scan.latest_scan()

if latest is None:
    st.info("No completed scan yet. Run one from the sidebar.")
    st.stop()

candidates = scan.candidates_for(latest["id"], include_near_misses=True)
passed = [c for c in candidates if c.get("rank") is not None]
near_misses = [c for c in candidates if c.get("rank") is None]

age_minutes = (datetime.now() - latest["run_at"]).total_seconds() / 60
st.caption(
    f"Scan #{latest['id']} - {latest['universe']} - "
    f"{latest['run_at'].strftime('%d %b %Y, %H:%M')} "
    f"({age_minutes:.0f} min ago, took {latest['duration_seconds']:.0f}s)"
)


# --- Funnel -----------------------------------------------------------------
# Shown because a scan returning nothing and a scan whose data feed broke look
# identical from the candidate list alone. The funnel tells them apart.

st.markdown("## Funnel")

total = latest["universe_size"] or 0
stages = [
    ("Universe", total, "every stock in the index"),
    ("Passed dip screen", latest["passed_dip"] or 0, "drawdown, RSI and the trend filter"),
    ("Passed quality gate", latest["passed_quality"] or 0, "fundamentals, of those that dipped"),
    ("Passed delivery", latest["passed_delivery"] or 0, "accumulation confirmed"),
    ("Candidates", len(passed), "cleared all three stages"),
]

cols = st.columns(len(stages))
for col, (label, value, hint) in zip(cols, stages):
    share = f"{value / total * 100:.1f}% of universe" if total else ""
    col.markdown(
        theme.stat(label, f"{value:,}", f"{hint} - {share}" if share else hint),
        unsafe_allow_html=True,
    )

if total and (latest["passed_dip"] or 0) == 0:
    st.warning(
        "Nothing passed even the dip screen. Either the market is at a high with no "
        "meaningful pullbacks, or the price feed is stale. Check a known symbol in Deep Dive."
    )


# --- Candidates -------------------------------------------------------------

st.markdown("## Candidates")

if not passed:
    st.info(
        "No stock cleared all three stages. This is a normal result near a market high. "
        "The near misses below show what came closest and exactly what stopped it."
    )
else:
    rows = []
    for candidate in passed:
        metrics = candidate.get("metrics", {})
        rows.append(
            {
                "Rank": candidate["rank"],
                "Symbol": candidate["symbol"],
                "Score": candidate["screen_score"],
                "Price": candidate["close"],
                "Off high %": candidate["drawdown_pct"],
                "RSI": candidate["rsi"],
                "vs 200DMA %": metrics.get("pct_vs_dma_long"),
                "DMA slope %": candidate["dma_slope_pct"],
                "Delivery x": candidate["delivery_ratio"],
                "Persist": candidate["delivery_persistence"],
                "ATR %": metrics.get("atr_pct"),
                "Sector": metrics.get("sector"),
            }
        )

    frame = pd.DataFrame(rows)
    st.dataframe(
        frame,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Rank": st.column_config.NumberColumn(width="small"),
            "Score": st.column_config.ProgressColumn(
                "Score", min_value=0, max_value=100, format="%.0f", width="small"
            ),
            "Price": st.column_config.NumberColumn(format="%.2f"),
            "Off high %": st.column_config.NumberColumn(
                format="%.1f", help="Percentage below the 52-week high"
            ),
            "RSI": st.column_config.NumberColumn(format="%.1f"),
            "vs 200DMA %": st.column_config.NumberColumn(
                format="%.1f", help="Above zero means trading above the 200-day average"
            ),
            "DMA slope %": st.column_config.NumberColumn(
                format="%.1f", help="6-month change in the 200-day average. Must not be falling."
            ),
            "Delivery x": st.column_config.NumberColumn(
                format="%.2f", help="Delivery on down days against its 20-day average"
            ),
            "Persist": st.column_config.NumberColumn(
                width="small", help="Sessions of the last 10 with elevated delivery"
            ),
            "ATR %": st.column_config.NumberColumn(format="%.1f", help="Daily volatility"),
        },
    )

    st.caption(
        "Score ranks the shortlist for attention; it is not a verdict. Everything listed "
        "already passed all three stages."
    )

    # The score does not predict outcomes on backtest evidence, so say so
    # here rather than letting the progress bars imply more than they mean.
    st.info(
        "**On the score:** bucketing backtest trades by this score gave the top bucket a "
        "51% win rate against 67% for the bottom - it did not predict outcomes. Treat it as "
        "a reading order, not a ranking of quality. **The committee's conviction is the "
        "number meant to predict** - open the Committee page for a verdict on any of these."
    )


# --- Near misses ------------------------------------------------------------

if show_near_misses and near_misses:
    st.markdown("## Near misses")
    st.caption(
        "These cleared the dip screen but failed later. Worth reading: the reason a stock "
        "was rejected is often more informative than the list of those that passed."
    )

    for candidate in near_misses[:15]:
        failures = candidate.get("failures", {}) or {}
        summary = failures.get("summary", "")

        with st.expander(
            f"{candidate['symbol']}  -  {theme.num(candidate['close'], 2)}  -  {summary[:90]}"
        ):
            left, right = st.columns([1, 2])
            with left:
                st.markdown(
                    theme.stat("Off 52w high", theme.pct(candidate["drawdown_pct"])),
                    unsafe_allow_html=True,
                )
                st.markdown(theme.stat("RSI", theme.num(candidate["rsi"], 1)), unsafe_allow_html=True)
            with right:
                if failures.get("quality"):
                    st.markdown("**Quality gate**", unsafe_allow_html=True)
                    st.markdown(theme.reasons(failures["quality"]), unsafe_allow_html=True)
                if failures.get("delivery"):
                    st.markdown("**Delivery**", unsafe_allow_html=True)
                    st.markdown(theme.reasons(failures["delivery"]), unsafe_allow_html=True)


# --- History ----------------------------------------------------------------

with st.expander("Scan history"):
    history = scan.scan_history(20)
    if history:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Run": h["run_at"].strftime("%d %b %H:%M"),
                        "Universe": h["universe"],
                        "Size": h["universe_size"],
                        "Dip": h["passed_dip"],
                        "Quality": h["passed_quality"],
                        "Delivery": h["passed_delivery"],
                        "Status": h["status"],
                        "Seconds": round(h["duration_seconds"] or 0),
                    }
                    for h in history
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )
