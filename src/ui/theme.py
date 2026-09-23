"""Shared styling and display helpers for the dashboard.

Design direction: OLED dark, dense spacing, Fira Sans for text and Fira Code
for every number.

Two choices worth explaining, because they are what make a financial table
readable rather than merely dark:

**Tabular numerals.** Proportional digits make columns of rupee figures
ragged, so the eye cannot compare magnitudes down a column. `tabular-nums`
fixes every digit to the same width, and a column of prices becomes scannable.

**Never colour alone.** Red and green are the obvious way to show gain and
loss, and roughly one man in twelve cannot reliably tell them apart. Every
value that uses colour also carries a sign or an arrow, so the meaning
survives without it.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

# Palette from the generated design system.
COLORS = {
    "bg": "#020617",
    "card": "#0E1223",
    "muted": "#1A1E2F",
    "border": "#334155",
    "fg": "#F8FAFC",
    "fg_muted": "#94A3B8",
    "accent": "#22C55E",
    "positive": "#22C55E",
    "negative": "#EF4444",
    "warning": "#F59E0B",
    "info": "#38BDF8",
    "neutral": "#64748B",
}

STANCE_COLORS = {
    "AGGRESSIVE": COLORS["accent"],
    "BALANCED": COLORS["info"],
    "CONSERVATIVE": COLORS["warning"],
    "NO_BUY": COLORS["negative"],
    "BUY": COLORS["accent"],
    "WATCH": COLORS["warning"],
    "HOLD": COLORS["neutral"],
    "TRIM": COLORS["warning"],
    "EXIT": COLORS["negative"],
    "REVIEW": COLORS["info"],
}

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500;600&family=Fira+Sans:wght@300;400;500;600;700&display=swap');

html, body, [class*="css"], .stMarkdown, .stText {
    font-family: 'Fira Sans', -apple-system, system-ui, sans-serif;
}

/* Every number is monospaced and tabular so columns line up and
   magnitudes can be compared by eye down a column. */
code, .stDataFrame, .stMetric, [data-testid="stMetricValue"],
[data-testid="stMetricDelta"], .mono {
    font-family: 'Fira Code', 'SF Mono', Menlo, monospace !important;
    font-variant-numeric: tabular-nums;
    font-feature-settings: "tnum";
}

/* Dense dashboard spacing: reclaim Streamlit's generous default padding. */
.block-container { padding-top: 2.2rem; padding-bottom: 3rem; max-width: 1500px; }
[data-testid="stSidebar"] { border-right: 1px solid #334155; }
[data-testid="stVerticalBlock"] { gap: 0.6rem; }
hr { margin: 0.8rem 0; border-color: #1A1E2F; }

h1 { font-size: 1.75rem !important; font-weight: 600; letter-spacing: -0.02em; margin-bottom: 0.2rem; }
h2 { font-size: 1.25rem !important; font-weight: 600; letter-spacing: -0.01em; margin-top: 1.2rem; }
h3 { font-size: 1.02rem !important; font-weight: 600; color: #CBD5E1; }

[data-testid="stMetric"] {
    background: #0E1223;
    border: 1px solid #1E2438;
    border-radius: 8px;
    padding: 0.75rem 0.9rem;
}
[data-testid="stMetricLabel"] {
    font-size: 0.72rem !important;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: #94A3B8 !important;
    font-family: 'Fira Sans', sans-serif !important;
}
[data-testid="stMetricValue"] { font-size: 1.35rem !important; font-weight: 500; }

.card {
    background: #0E1223;
    border: 1px solid #1E2438;
    border-radius: 10px;
    padding: 1rem 1.15rem;
    margin-bottom: 0.7rem;
}
.card-tight { padding: 0.7rem 0.9rem; }

.pill {
    display: inline-block;
    padding: 0.16rem 0.6rem;
    border-radius: 999px;
    font-size: 0.72rem;
    font-weight: 600;
    letter-spacing: 0.04em;
    font-family: 'Fira Sans', sans-serif;
}

.label {
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: #94A3B8;
}

.disclaimer {
    background: rgba(245, 158, 11, 0.07);
    border-left: 3px solid #F59E0B;
    border-radius: 4px;
    padding: 0.55rem 0.85rem;
    font-size: 0.8rem;
    color: #CBD5E1;
    margin-bottom: 1rem;
}

.reason {
    font-size: 0.82rem;
    color: #94A3B8;
    padding: 0.28rem 0 0.28rem 0.75rem;
    border-left: 2px solid #334155;
    margin-bottom: 0.2rem;
}
.reason-fail { border-left-color: #EF4444; }
.reason-pass { border-left-color: #22C55E; }
.reason-warn { border-left-color: #F59E0B; }

.stDataFrame { border: 1px solid #1E2438; border-radius: 8px; }
.stButton button { border-radius: 6px; font-weight: 500; transition: all 160ms ease; }
.stButton button:hover { transform: translateY(-1px); }
.stTabs [data-baseweb="tab"] { font-size: 0.88rem; font-weight: 500; }

/* Keyboard focus must stay visible - never remove the ring. */
*:focus-visible { outline: 2px solid #22C55E !important; outline-offset: 2px; }

@media (prefers-reduced-motion: reduce) {
    * { animation: none !important; transition: none !important; }
    .stButton button:hover { transform: none; }
}
</style>
"""


def apply(page_title: str = "Dip Committee", icon: str = "chart_with_upwards_trend") -> None:
    """Set page config and inject the stylesheet. Call once per page, first."""
    st.set_page_config(
        page_title=page_title,
        page_icon=":" + icon + ":",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    st.markdown(CSS, unsafe_allow_html=True)


def disclaimer() -> None:
    """The standing notice, on every page that shows a verdict."""
    st.markdown(
        '<div class="disclaimer"><strong>Research output, not investment advice.</strong> '
        "This tool is not run by a SEBI-registered adviser. Everything here is evidence and "
        "probability, weighed and shown with its reasoning - the decision to buy or sell is yours."
        "</div>",
        unsafe_allow_html=True,
    )


# --- Formatting -------------------------------------------------------------


def rupees(value: Any, decimals: int = 0) -> str:
    """Format in the Indian numbering system: 12,34,567 rather than 1,234,567."""
    if value is None:
        return "--"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "--"

    negative = number < 0
    number = abs(number)
    whole = int(number)
    fraction = number - whole

    digits = str(whole)
    if len(digits) > 3:
        last3 = digits[-3:]
        rest = digits[:-3]
        groups = []
        while len(rest) > 2:
            groups.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            groups.insert(0, rest)
        formatted = ",".join(groups + [last3])
    else:
        formatted = digits

    if decimals:
        formatted += f".{fraction:.{decimals}f}"[2:].ljust(decimals, "0")
        formatted = formatted if "." in formatted else formatted

    text = f"Rs {formatted}"
    return f"-{text}" if negative else text


def crores(value: Any) -> str:
    if value is None:
        return "--"
    try:
        return f"Rs {float(value):,.0f} cr"
    except (TypeError, ValueError):
        return "--"


def pct(value: Any, decimals: int = 1, signed: bool = False) -> str:
    if value is None:
        return "--"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "--"
    return f"{number:+.{decimals}f}%" if signed else f"{number:.{decimals}f}%"


def num(value: Any, decimals: int = 2) -> str:
    if value is None:
        return "--"
    try:
        return f"{float(value):,.{decimals}f}"
    except (TypeError, ValueError):
        return "--"


def coloured(value: Any, *, decimals: int = 1, suffix: str = "%", invert: bool = False) -> str:
    """A signed, coloured value that still reads correctly without colour.

    The arrow and the sign carry the meaning; the colour only reinforces it.
    """
    if value is None:
        return '<span style="color:#64748B">--</span>'
    try:
        number = float(value)
    except (TypeError, ValueError):
        return '<span style="color:#64748B">--</span>'

    good = number < 0 if invert else number > 0
    colour = COLORS["positive"] if good else (COLORS["negative"] if number else COLORS["neutral"])
    arrow = "▲" if number > 0 else ("▼" if number < 0 else "—")

    return (
        f'<span class="mono" style="color:{colour}">{arrow} {number:+.{decimals}f}{suffix}</span>'
    )


def pill(text: str, colour: str | None = None) -> str:
    colour = colour or STANCE_COLORS.get(str(text).upper(), COLORS["neutral"])
    return (
        f'<span class="pill" style="background:{colour}1f;color:{colour};'
        f'border:1px solid {colour}55">{text}</span>'
    )


def reasons(items: list[str], kind: str = "fail") -> str:
    if not items:
        return ""
    return "".join(f'<div class="reason reason-{kind}">{item}</div>' for item in items)


def stat(label: str, value: str, hint: str | None = None) -> str:
    """A compact label/value pair for dense metric rows."""
    hint_html = f'<div style="font-size:0.7rem;color:#64748B">{hint}</div>' if hint else ""
    return (
        f'<div class="card card-tight"><div class="label">{label}</div>'
        f'<div class="mono" style="font-size:1.1rem;color:#F8FAFC">{value}</div>'
        f"{hint_html}</div>"
    )
