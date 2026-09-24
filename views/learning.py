"""Learning - what the committee has learned from its own results."""

from __future__ import annotations

import pandas as pd
import streamlit as st
from sqlalchemy import select

from src import db
from src.config import load_config
from src.learning import checkpoints, proposals, scorecards, weights

cfg = load_config()
db.init_db()

st.markdown("# Learning")
st.caption(
    "After every recommendation the committee checks what the stock actually did against the "
    "Nifty 500, scores each bot on it, and slowly shifts weight toward the desks that were right. "
    "Rule changes are only ever suggested here, for you to decide."
)

min_n = int(cfg.get("learning.min_observations", 30))
primary = int(cfg.get("learning.primary_horizon_days", 90))

# ------------------------------------------------------------------ Progress --
maturity = checkpoints.maturity(cfg)
cols = st.columns(len(maturity) or 1)
for col, (horizon, count) in zip(cols, sorted(maturity.items())):
    col.metric(f"Outcomes at {horizon} days", count)

have = maturity.get(primary, 0)
if have < min_n:
    st.info(
        f"The learner needs {min_n} recommendations with a {primary}-day outcome before it may move "
        f"any weight. It has {have}. Until then it measures and reports, and changes nothing."
    )

# ---------------------------------------------------------------- Scorecards --
st.markdown("## How each bot has done")
cards = scorecards.latest()
if not cards:
    st.caption("No outcomes have matured yet. The first arrive 30 days after a recommendation.")
else:
    frame = pd.DataFrame(cards)
    frame = frame[frame["horizon_days"] == primary] if (frame["horizon_days"] == primary).any() else frame
    frame["hit_rate"] = frame["hit_rate"] * 100.0
    st.dataframe(
        frame[["bot_id", "desk", "horizon_days", "n_observations", "information_coefficient",
               "ic_p_value", "hit_rate", "verdict"]].rename(columns={
            "bot_id": "Bot", "desk": "Desk", "horizon_days": "Days", "n_observations": "Outcomes",
            "information_coefficient": "IC", "ic_p_value": "p", "hit_rate": "Right direction",
            "verdict": "Verdict"}),
        use_container_width=True, hide_index=True,
        column_config={
            "IC": st.column_config.NumberColumn(format="%+.3f",
                                                help="Rank correlation between the bot's score and the "
                                                     "stock's return against the index. Above 0.05 is useful."),
            "p": st.column_config.NumberColumn(format="%.2f"),
            "Right direction": st.column_config.NumberColumn(format="%.0f%%"),
        },
    )

# ------------------------------------------------------------------- Weights --
st.markdown("## Desk weights")
active = weights.active_weights(cfg)
st.dataframe(pd.DataFrame([{"Desk": d, "Weight": w} for d, w in active.items()]),
             hide_index=True, use_container_width=False)

versions = weights.history()
if versions:
    with st.expander(f"History ({len(versions)} version{'s' if len(versions) != 1 else ''})"):
        for v in versions:
            desk_weights = (v.get("payload") or {}).get("desk_weights", {})
            status = "active" if v.get("is_active") else ("not applied - failed the out-of-sample check"
                                                          if v.get("source") == "learner" and not v.get("passed_oos_gate")
                                                          else "")
            st.markdown(
                f"**Version {v['id']}** ({v['source']}, {v['created_at']:%d %b %Y}) {status}  \n"
                + ", ".join(f"{d} {w:.2f}" for d, w in desk_weights.items())
            )
            if v.get("rationale"):
                st.caption(v["rationale"])
            if not v.get("is_active") and desk_weights:
                if st.button(f"Restore version {v['id']}", key=f"restore-{v['id']}"):
                    weights.restore(int(v["id"]))
                    st.success(f"Version {v['id']} is active again.")
                    st.rerun()

# ------------------------------------------------------------------- Lessons --
st.markdown("## Lessons from closed trades")
with db.connection() as conn:
    lessons = [dict(r._mapping) for r in conn.execute(
        select(db.lessons).order_by(db.lessons.c.id.desc()).limit(50)).fetchall()]
if not lessons:
    st.caption("No trade has closed yet. Every closed trade, win or loss, is reviewed here.")
for lesson in lessons:
    tag = "WIN" if lesson.get("outcome") == "win" else "LOSS"
    st.markdown(f"**{lesson['symbol']}** · {tag} · {lesson['category']}  \n{lesson['lesson']}")

# ----------------------------------------------------------------- Proposals --
st.markdown("## Suggested rule changes")
pending = proposals.listing("pending")
if not pending:
    st.caption(
        f"None yet. A change is suggested only when at least {proposals.MIN_TRADES} closed trades show "
        f"an alternative exit doing clearly better across winners and losers."
    )
for p in pending:
    st.markdown(f"**{p['config_path']}**: {p['current_value']} -> {p['proposed_value']}  \n{p['rationale']}")
    left, right = st.columns(2)
    if left.button("Approve", key=f"approve-{p['id']}"):
        proposals.decide(int(p["id"]), approve=True)
        st.success("Approved. The next weekly run opens a pull request; merging it applies the change.")
        st.rerun()
    if right.button("Reject", key=f"reject-{p['id']}"):
        proposals.decide(int(p["id"]), approve=False)
        st.rerun()

decided = [p for p in proposals.listing() if p["status"] != "pending"]
if decided:
    with st.expander("Decided"):
        for p in decided:
            st.caption(f"{p['config_path']} -> {p['proposed_value']}: {p['status']}")
