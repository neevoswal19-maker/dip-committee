"""Committee - the full 23-bot report for one stock."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import streamlit as st

from src import committee as committee_module
from src import scan
from src.agents import registry
from src.agents.schemas import CommitteeReport
from src.config import load_config
from src.ui import theme

cfg = load_config()

st.markdown("# Committee")
st.caption(
    "Seventeen analysts across five desks, five desk heads, and the Chief Market "
    "Intelligence Officer. All rule-based, so it runs in seconds and costs nothing."
)

theme.disclaimer()


# --- Choose a stock ---------------------------------------------------------

_, candidates = scan.latest_candidates(include_near_misses=True)
suggested = [c["symbol"] for c in candidates]

with st.sidebar:
    st.markdown("### Run the committee")
    if suggested:
        picked = st.selectbox("From the latest scan", ["(type my own)"] + suggested)
    else:
        picked = "(type my own)"

    typed = st.text_input(
        "Symbol", value="" if picked != "(type my own)" else "INDIANB"
    ).strip().upper()
    symbol = typed if picked == "(type my own)" else picked

    capital = st.number_input(
        "Capital (Rs)", min_value=10_000, max_value=100_000_000,
        value=int(cfg.get("sizing.default_capital", 500_000)), step=50_000,
    )

    run_now = st.button("Run committee", type="primary", use_container_width=True, disabled=not symbol)

    st.divider()
    st.caption(
        f"{registry.bot_count()} bots. Rule-based, so every verdict is reproducible "
        f"from its inputs - which is what lets the learning loop measure whether each "
        f"bot's score actually predicts anything."
    )


if run_now and symbol:
    from src.strategy import sizing as sizing_rules

    bar = st.progress(0.0, text="Gathering evidence...")

    def report_progress(label: str, fraction: float) -> None:
        bar.progress(min(max(fraction, 0.0), 1.0), text=label)

    try:
        with st.spinner(f"Running the committee on {symbol}..."):
            report = committee_module.run(
                symbol, cfg=cfg,
                portfolio=sizing_rules.PortfolioState(capital=float(capital)),
                progress=report_progress,
            )
        bar.empty()
        st.session_state["committee_report"] = report
    except Exception as exc:
        bar.empty()
        st.error(f"The committee run failed: {exc}")


report: CommitteeReport | None = st.session_state.get("committee_report")

if report is None:
    st.info("Pick a stock in the sidebar and run the committee.")

    st.markdown("## The org chart")
    chart = pd.DataFrame(registry.describe())
    for desk in registry.DESK_ORDER:
        rows = chart[chart["desk"] == desk]
        from src.agents.leads import DESK_NAMES

        st.markdown(f"**{DESK_NAMES.get(desk, desk)}**")
        st.markdown(
            "".join(f'<div class="reason">{r["name"]}</div>' for _, r in rows.iterrows()),
            unsafe_allow_html=True,
        )
    st.stop()


# --- The verdict ------------------------------------------------------------

stance_colour = {
    "BUY": theme.COLORS["positive"],
    "WATCH": theme.COLORS["warning"],
    "NO_BUY": theme.COLORS["negative"],
}.get(report.stance, theme.COLORS["neutral"])

head = st.columns([2, 1, 1, 1, 1])
head[0].markdown(
    f"## {report.symbol}\n<span class='label'>{report.sector or ''} &middot; "
    f"Rs {report.price:,.2f} &middot; {report.run_at.strftime('%d %b %H:%M')}</span>",
    unsafe_allow_html=True,
)
head[1].metric("Conviction", f"{report.conviction:.0f}")
head[2].metric("Stance", report.stance.replace("_", " "))
head[3].metric("Coverage", f"{report.coverage:.0%}")
head[4].metric("Ran in", f"{report.duration_seconds:.1f}s")

if report.forensics_veto:
    st.error(f"**Forensic veto.** {report.summary}")
else:
    st.markdown(
        f'<div class="card" style="border-left:3px solid {stance_colour};border-radius:0">'
        f'<div style="font-size:0.95rem;line-height:1.65">{report.summary}</div></div>',
        unsafe_allow_html=True,
    )

if report.red_flags:
    with st.expander(f"{len(report.red_flags)} red flag(s) raised"):
        st.markdown(theme.reasons(report.red_flags, "fail"), unsafe_allow_html=True)

st.markdown("#### What the committee overruled")
st.caption("The minority view is where the next mistake usually comes from, so it is named rather than averaged away.")
st.code(report.dissent, language=None)


# --- Sizing -----------------------------------------------------------------

sizing = report.sizing or {}
if sizing.get("is_buy"):
    st.markdown("## Position")
    cols = st.columns(4)
    cols[0].markdown(
        theme.stat("Recommendation", sizing["recommendation"]), unsafe_allow_html=True
    )
    cols[1].markdown(
        theme.stat("Size", theme.rupees(sizing["total_value"]), f"{sizing['total_shares']:,} shares"),
        unsafe_allow_html=True,
    )
    cols[2].markdown(
        theme.stat("Stop", theme.num(sizing.get("stop_price"), 2),
                   f"risking {theme.rupees(sizing.get('risk_amount', 0))}"),
        unsafe_allow_html=True,
    )
    cols[3].markdown(
        theme.stat("Of capital", theme.pct(sizing.get("pct_of_capital")),
                   f"bound by {sizing.get('binding_constraint', '')}"),
        unsafe_allow_html=True,
    )

    if sizing.get("tranches"):
        st.dataframe(
            pd.DataFrame([{
                "Tranche": t["sequence"],
                "Share": f"{t['pct_of_position']:.0f}%",
                "Value": theme.rupees(t["value"]),
                "Shares": t["shares"],
                "Trigger": t["description"],
            } for t in sizing["tranches"]]),
            use_container_width=True, hide_index=True,
        )
elif sizing.get("rejections"):
    st.markdown("## Position")
    st.markdown(theme.reasons(sizing["rejections"], "fail"), unsafe_allow_html=True)


# --- The desks --------------------------------------------------------------

st.markdown("## The desks")

for desk in report.desks:
    colour = (
        theme.COLORS["positive"] if desk.score > 1
        else (theme.COLORS["negative"] if desk.score < -1 else theme.COLORS["neutral"])
    )
    blind = f" &middot; {len(desk.blind_analysts)} blind" if desk.blind_analysts else ""

    with st.expander(
        f"{desk.name}   |   score {desk.score:+.2f}   confidence {desk.confidence:.2f}"
        f"   coverage {desk.coverage:.0%}",
        expanded=abs(desk.score) > 1.5,
    ):
        st.markdown(
            f'<div style="font-size:0.88rem;color:#CBD5E1;line-height:1.6;'
            f'border-left:2px solid {colour};padding-left:0.7rem;margin-bottom:0.8rem">'
            f"{desk.summary}</div>",
            unsafe_allow_html=True,
        )

        if desk.dissent:
            st.markdown("**Dissent within the desk**")
            st.markdown(theme.reasons(desk.dissent, "warn"), unsafe_allow_html=True)

        for verdict in desk.verdicts:
            if not verdict.data_available:
                st.markdown(
                    f'<div class="card card-tight" style="opacity:0.6">'
                    f'<strong>{verdict.name}</strong> {theme.pill("NO DATA", theme.COLORS["neutral"])}'
                    f'<div style="font-size:0.8rem;color:#64748B;margin-top:0.3rem">'
                    f"{verdict.data_note}</div></div>",
                    unsafe_allow_html=True,
                )
                continue

            score_colour = (
                theme.COLORS["positive"] if verdict.score > 0.5
                else (theme.COLORS["negative"] if verdict.score < -0.5 else theme.COLORS["neutral"])
            )
            findings = "".join(
                f'<div style="font-size:0.83rem;color:#CBD5E1;margin-top:0.25rem">{f}</div>'
                for f in verdict.key_findings
            )
            evidence = " &middot; ".join(
                f"{e.field}={e.value}" for e in verdict.evidence if e.value is not None
            )
            flags = "".join(
                f'<div style="font-size:0.8rem;color:{theme.COLORS["negative"]};margin-top:0.25rem">'
                f"! {f}</div>" for f in verdict.red_flags
            )

            st.markdown(
                f'<div class="card card-tight">'
                f'<div style="display:flex;gap:0.6rem;align-items:baseline">'
                f'<strong>{verdict.name}</strong>'
                f'<span class="mono" style="color:{score_colour};font-size:0.95rem">'
                f"{verdict.score:+.1f}</span>"
                f'<span class="label">confidence {verdict.confidence:.2f}</span>'
                f'{" " + theme.pill("VETO", theme.COLORS["negative"]) if verdict.veto else ""}'
                f"</div>{findings}{flags}"
                f'<div style="font-size:0.72rem;color:#64748B;margin-top:0.4rem">{evidence}</div>'
                f"</div>",
                unsafe_allow_html=True,
            )


# --- Exit doctrine ----------------------------------------------------------

if report.exit_doctrine:
    st.markdown("## Exit doctrine")
    st.caption("Written now, checked every day, so the decision to sell is not made while watching a position fall.")
    doctrine = report.exit_doctrine
    lines = [f"Exit doctrine for {report.symbol}:"]
    for target in doctrine.get("targets", []):
        lines.append(f"  - Trim {target['trim_pct']:.0f}% at +{target['gain_pct']:.0f}% (Rs {target['price']:,.2f})")
    trailing = doctrine.get("trailing_stop", {})
    if trailing:
        lines.append(
            f"  - Once up {trailing.get('activate_after_gain_pct', 30):.0f}%, trail "
            f"{trailing.get('trail_pct', 20):.0f}% below the highest close"
        )
    lines.append(f"  - Hard stop at {doctrine.get('hard_stop_pct', -25):.0f}% forces a re-review, not a sale")
    lines.append("  - Sell regardless of price if any of these break:")
    lines.extend(f"      * {rule}" for rule in doctrine.get("thesis_break_rules", []))
    lines.append(f"  - {doctrine.get('tax_note', '')}")
    st.code("\n".join(lines), language=None)


# --- Past runs --------------------------------------------------------------

with st.expander("Past committee runs"):
    runs = committee_module.recent_runs(20)
    if runs:
        st.dataframe(
            pd.DataFrame([{
                "Run": r["run_at"].strftime("%d %b %H:%M"),
                "Symbol": r["symbol"],
                "Conviction": r["conviction"],
                "Stance": r["stance"],
                "Recommendation": r["recommendation"],
                "Veto": r["forensics_veto"],
                "Seconds": round(r["duration_seconds"] or 0, 1),
            } for r in runs]),
            use_container_width=True, hide_index=True,
        )
    else:
        st.caption("No stored runs yet.")
