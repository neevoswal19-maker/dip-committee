"""Intraday - the ORB + VWAP pullback strategy on NIFTYBEES and BANKBEES.

A separate strategy from the dip committee. It runs in paper mode until 40
trades have been judged against a bar fixed in advance.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st
from sqlalchemy import select

from src import db
from src.config import load_config
from src.intraday import orb

cfg = load_config()
db.init_db()
ic = cfg.get("intraday", {}) or {}
mode = str(ic.get("mode", "paper"))
capital = float(ic.get("capital", 500000))

st.markdown("# Intraday")
st.caption(
    "Opening-range breakout, then a pullback to VWAP and the 5-minute 20 EMA, on NIFTYBEES (Nifty 50) "
    "and BANKBEES (Nifty Bank). Separate from the dip strategy: its own capital, rules and alerts."
)

with db.connection() as conn:
    rows = [dict(r._mapping) for r in conn.execute(
        select(db.intraday_trades).where(db.intraday_trades.c.mode == mode)
        .order_by(db.intraday_trades.c.entry_at.desc()))]
for row in rows:
    row["closed"] = bool(row.get("closed"))

card = orb.scorecard(rows, capital)
verdict, why = orb.judge(card)

st.markdown(f"## {'Paper' if mode == 'paper' else 'Live'} test")
cols = st.columns(4)
cols[0].metric("Trades", f"{card.get('trades', 0)} / {orb.PASS['min_trades']}")
cols[1].metric("Win rate", f"{card['win_rate']:.0%}" if card.get("trades") else "-")
cols[2].metric("Profit factor", f"{card['profit_factor']:.2f}" if card.get("trades") else "-")
cols[3].metric("Net after costs", f"Rs {card['net']:,.0f}" if card.get("trades") else "-")

bar = (f"The bar, fixed in advance: at least {orb.PASS['min_trades']} trades, profit factor at least "
       f"{orb.PASS['min_profit_factor']}, a positive average trade after costs, and a worst drawdown no "
       f"deeper than {orb.PASS['max_drawdown_pct']:.0f}% of Rs {capital:,.0f}.")
if verdict is None:
    st.info(f"{bar} So far: {'; '.join(why)}.")
elif verdict:
    st.success(f"{bar} It passed. Set intraday.mode to live to send these as live calls.")
else:
    st.error(f"{bar} It failed ({'; '.join(why)}), so it should be removed.")

st.markdown("## Trades")
if rows:
    table = pd.DataFrame(rows)[["day", "symbol", "side", "entry", "stop", "units", "exit_reason", "net", "r", "closed"]]
    table.columns = ["Day", "ETF", "Side", "Entry", "Stop", "Units", "Exit", "Net (Rs)", "R", "Closed"]
    st.dataframe(table, hide_index=True, use_container_width=True)
else:
    st.caption("No intraday trades yet. The live job runs each weekday from 9:15 to 15:12.")

st.caption(
    "Each day's 5-minute bars are stored, so the weekly backtest (Intraday research on GitHub) covers "
    "more sessions over time than the 60 Yahoo keeps."
)
