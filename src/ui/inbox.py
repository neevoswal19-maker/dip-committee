"""Record trades reported to the Telegram bot when a page loads.

The 15-minute job does this too; running it on page load means a purchase
messaged a minute ago is already on the page when the owner opens it.
Streamlit re-runs the whole script on every click, so this checks at most
once a minute per browser session.
"""

from __future__ import annotations

import time

import streamlit as st

from src.alerts import telegram_inbox

_KEY = "_telegram_inbox_checked_at"


def check_for_trades() -> None:
    try:
        last = st.session_state.get(_KEY, 0.0)
        if time.time() - last < 60:
            return
        st.session_state[_KEY] = time.time()
        outcomes = telegram_inbox.process_pending()
    except Exception as exc:  # never let the inbox break a page
        st.caption(f"Couldn't check Telegram for new trades ({type(exc).__name__}).")
        return

    recorded = [o for o in outcomes if o.get("status") == "recorded"]
    undone = [o for o in outcomes if o.get("status") == "undone"]
    if recorded:
        st.success(
            f"Recorded {len(recorded)} trade{'s' if len(recorded) != 1 else ''} "
            f"from your Telegram messages."
        )
    if undone:
        st.info(f"Undid {len(undone)} trade{'s' if len(undone) != 1 else ''} at your request on Telegram.")
