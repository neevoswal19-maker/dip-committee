"""Overview - the first thing you see: what the last scan found."""

from __future__ import annotations

from datetime import datetime

import streamlit as st

from src import db, scan
from src.config import load_config
from src.strategy import regime as regime_rules
from src.ui import theme

cfg = load_config()
db.init_db()


@st.cache_data(ttl=3600, show_spinner=False)
def _market_regime() -> dict:
    return regime_rules.current(cfg).to_dict()

st.markdown("# Dip Committee")
st.caption(
    f"Buy-the-dip research for the {cfg.get('universe.index')} - "
    f"delivery-confirmed and trend-filtered by the screen, then judged by the committee."
)

theme.disclaimer()

latest = scan.latest_scan()

if latest is None:
    st.info(
        "No scan has been run yet. Open **Screener** and run one - it takes about a "
        "minute for the full universe."
    )
else:
    age = datetime.now() - latest["run_at"]
    hours = age.total_seconds() / 3600
    freshness = (
        f"{int(age.total_seconds() // 60)} min ago" if hours < 1
        else (f"{hours:.0f} hours ago" if hours < 48 else f"{age.days} days ago")
    )

    _, candidates = scan.latest_candidates()

    cols = st.columns(5)
    cols[0].metric("Candidates", len(candidates))
    cols[1].metric("Universe", latest["universe_size"] or 0)
    cols[2].metric("Dip screen", latest["passed_dip"] or 0)
    cols[3].metric("Delivery", latest["passed_delivery"] or 0)
    cols[4].metric("Last scan", freshness)

    market = _market_regime()
    label = market.get("label", "UNKNOWN")
    regime_text = regime_rules.describe_for_humans(regime_rules.MarketRegime(
        **{**market, "as_of": None}
    ))
    if label == "DOWNTREND":
        st.warning(regime_text)
    elif label == "UNKNOWN":
        st.info(regime_text)
    else:
        st.caption(regime_text)

    if hours > 36:
        st.warning(
            f"The most recent scan ran {freshness}. Prices and delivery have moved on - "
            f"run a fresh one before acting on anything below."
        )

    st.markdown("## Today's candidates")
    st.caption(
        "These passed the three-stage screen. The screen decides what is worth "
        "examining; the committee decides what it thinks."
    )

    if not candidates:
        st.info(
            "The screen found nothing today. There is no rule that a dip must exist on any "
            "given day. The Screener page shows what came closest and exactly what "
            "stopped it."
        )
    else:
        verdicts = scan.convictions_for([c["symbol"] for c in candidates])
        unassessed = [c["symbol"] for c in candidates if c["symbol"] not in verdicts]

        if unassessed:
            st.warning(
                f"The committee has not assessed {', '.join(unassessed)}. Passing the screen "
                f"means a stock is worth looking at, not that anything has judged it. "
                f"Run the committee from its page, or re-run the scan to do it automatically."
            )

        for candidate in candidates[:6]:
            metrics = candidate.get("metrics", {})
            verdict = verdicts.get(candidate["symbol"])
            head, body = st.columns([1, 3.2])

            with head:
                st.markdown(
                    f"### {candidate['symbol']}\n"
                    f"<span class='label'>"
                    f"{metrics['sector'] if metrics.get('sector') else 'screened candidate'}</span>",
                    unsafe_allow_html=True,
                )
                st.markdown(
                    f'<div class="mono" style="font-size:1.3rem;margin-top:0.2rem">'
                    f'{theme.rupees(candidate["close"], 2)}</div>',
                    unsafe_allow_html=True,
                )

                # The committee verdict leads. The screen score is shown small
                # and labelled, because it is a reading order rather than a
                # judgement - measured at a 51% win rate for its top bucket
                # against 67% for its bottom.
                if verdict:
                    colour = theme.STANCE_COLORS.get(verdict["stance"], theme.COLORS["neutral"])
                    st.markdown(
                        f'<div style="margin-top:0.4rem">{theme.pill(verdict["stance"].replace("_", " "), colour)}'
                        f'<span class="mono" style="margin-left:0.5rem;color:{colour}">'
                        f'conviction {verdict["conviction"]:.0f}</span></div>'
                        f'<div style="font-size:0.68rem;color:#64748B;margin-top:0.25rem">'
                        f'committee, {verdict["run_at"]:%d %b %H:%M} &middot; '
                        f'screen rank score {candidate["screen_score"]:.0f}</div>',
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(
                        f'<div style="margin-top:0.4rem">'
                        f'{theme.pill("NOT ASSESSED", theme.COLORS["neutral"])}</div>'
                        f'<div style="font-size:0.68rem;color:#64748B;margin-top:0.25rem">'
                        f'screen rank score {candidate["screen_score"]:.0f} &middot; '
                        f'no committee verdict</div>',
                        unsafe_allow_html=True,
                    )

            with body:
                stats = st.columns(4)
                stats[0].markdown(
                    theme.stat("Off 52w high", theme.pct(candidate["drawdown_pct"])),
                    unsafe_allow_html=True,
                )
                stats[1].markdown(
                    theme.stat("RSI (14)", theme.num(candidate["rsi"], 1), "oversold below 40"),
                    unsafe_allow_html=True,
                )
                stats[2].markdown(
                    theme.stat(
                        "200 DMA slope",
                        theme.pct(candidate["dma_slope_pct"]),
                        "must be rising",
                    ),
                    unsafe_allow_html=True,
                )
                stats[3].markdown(
                    theme.stat(
                        "Delivery on dips",
                        f"{candidate['delivery_ratio']:.2f}x" if candidate["delivery_ratio"] else "--",
                        f"{candidate['delivery_persistence'] or 0} of last 10 sessions",
                    ),
                    unsafe_allow_html=True,
                )
            st.divider()

        if len(candidates) > 6:
            st.caption(f"{len(candidates) - 6} more on the Screener page.")


with st.sidebar:
    st.markdown("### Dip Committee")
    st.caption("Research tool. Not advice.")
    st.divider()

    st.markdown('<span class="label">The screen</span>', unsafe_allow_html=True)
    st.markdown(
        f"""
- Drawdown **{cfg.get('dip.drawdown_min_pct'):.0f}-{cfg.get('dip.drawdown_max_pct'):.0f}%**
  off the 52-week high
- The **{cfg.get('dip.dma_long')} DMA must be rising**, and price within
  {cfg.get('dip.dma_tolerance_below_pct'):.0f}% below it
- RSI below **{cfg.get('dip.rsi_max'):.0f}**
- Delivery on down days above
  **{cfg.get('delivery.down_day_delivery_ratio_min'):.2f}x**, for
  **{cfg.get('delivery.persistence_sessions_required')}+** sessions
        """
    )

    st.divider()
    st.markdown('<span class="label">Backtested</span>', unsafe_allow_html=True)
    st.markdown(
        """
2017-2023, a window including the 2018-19
correction and COVID:

**18.4%** CAGR against **14.1%** for the
index, drawdown **-27.8%** against **-38.3%**.
        """
    )
    st.caption("Flattered by survivorship bias. See Backtest for the caveats.")
