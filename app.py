"""Dip Committee - dashboard entry point and router.

Run locally with:  streamlit run app.py

The router owns page config, the stylesheet and the access gate, so each view
under views/ is just content. That also gives the navigation proper labels
rather than Streamlit's filename-derived ones.
"""

from __future__ import annotations

import secrets
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.config import dashboard_access_mode, dashboard_password
from src.ui import theme

theme.apply("Dip Committee")


def authenticated() -> bool:
    """Password gate for the deployed dashboard.

    The repository is public and the URL is guessable. The code being public
    is fine; a live view of someone's positions is not.

    The gate fails closed. `dashboard_access_mode()` returns 'refuse' whenever
    a password is absent and the environment is not provably a local
    development machine, so a mistyped or forgotten secret locks the app
    rather than opening it.
    """
    mode = dashboard_access_mode()

    if mode == "open":
        return True

    if mode == "refuse":
        st.error(
            "**DASHBOARD_PASSWORD is not set.** This looks like a deployed "
            "environment, so the dashboard will not serve without one - the URL is "
            "public and your positions are not."
        )
        st.caption(
            "Set DASHBOARD_PASSWORD in the app's secrets. If this really is your own "
            "machine, set ALLOW_INSECURE_LOCAL=1 to skip the password."
        )
        st.stop()

    if st.session_state.get("authenticated"):
        return True

    left, middle, right = st.columns([1, 1.1, 1])
    with middle:
        st.markdown("<div style='height:12vh'></div>", unsafe_allow_html=True)
        st.markdown("## Dip Committee")
        st.caption("Enter the dashboard password to continue.")
        with st.form("login"):
            entered = st.text_input("Password", type="password", label_visibility="collapsed")
            if st.form_submit_button("Unlock", use_container_width=True, type="primary"):
                # compare_digest rather than ==, so the comparison takes the
                # same time whatever the input. Overkill for one user, but
                # this is the only lock on the door.
                expected = dashboard_password() or ""
                if entered and secrets.compare_digest(entered, expected):
                    st.session_state["authenticated"] = True
                    st.rerun()
                else:
                    st.error("Incorrect password.")
    return False


if not authenticated():
    st.stop()


navigation = st.navigation(
    [
        st.Page("views/overview.py", title="Overview", default=True),
        st.Page("views/screener.py", title="Screener"),
        st.Page("views/deep_dive.py", title="Deep Dive"),
        st.Page("views/committee.py", title="Committee"),
        st.Page("views/portfolio.py", title="Portfolio"),
        st.Page("views/learning.py", title="Learning"),
        st.Page("views/backtest_page.py", title="Backtest"),
    ]
)
navigation.run()
