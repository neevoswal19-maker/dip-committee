"""Word lists and lookup tables the news desk reasons with.

This is the honest substitute for reading prose. A finance lexicon over
headlines catches direction reliably and nuance not at all - it cannot tell
"profit falls less than feared" from "profit falls", and it will read
"cuts debt" as negative unless told otherwise. The bots that use it say so in
their findings rather than presenting a keyword count as comprehension.

Everything here is a table so it can be extended without touching logic, and
so a future LLM body for those bots has something concrete to be measured
against.
"""

from __future__ import annotations

import re
from typing import Any

# --- Sentiment --------------------------------------------------------------
# Weighted rather than binary: "fraud" and "slightly below estimates" are not
# the same news, and an unweighted count would treat them alike.

POSITIVE_TERMS: dict[str, float] = {
    "record": 1.5, "beats": 2.0, "beat": 2.0, "surge": 1.5, "surges": 1.5,
    "jumps": 1.2, "rally": 1.2, "rallies": 1.2, "growth": 1.0, "grows": 1.0,
    "profit": 1.0, "profits": 1.0, "upgrade": 2.0, "upgrades": 2.0,
    "outperform": 1.8, "buy rating": 2.0, "expansion": 1.2, "expands": 1.2,
    "wins": 1.5, "won": 1.3, "bags": 1.5, "order win": 2.0, "contract": 1.0,
    "approval": 1.3, "approved": 1.3, "launch": 0.8, "launches": 0.8,
    "dividend": 1.0, "bonus": 1.0, "buyback": 1.5, "stake buy": 1.5,
    "turnaround": 1.5, "recovery": 1.2, "improves": 1.0, "improved": 1.0,
    "strong": 1.2, "robust": 1.2, "healthy": 1.0, "upbeat": 1.2,
    "debt reduction": 1.8, "deleveraging": 1.5, "margin expansion": 1.8,
    "capacity addition": 1.2, "acquisition": 0.8, "partnership": 1.0,
}

NEGATIVE_TERMS: dict[str, float] = {
    "fraud": 3.0, "scam": 3.0, "probe": 2.0, "investigation": 2.0,
    "raid": 2.5, "penalty": 1.8, "fine": 1.3, "default": 2.8,
    "insolvency": 3.0, "bankruptcy": 3.0, "resignation": 1.8, "resigns": 1.8,
    "quits": 1.5, "downgrade": 2.0, "downgrades": 2.0, "underperform": 1.8,
    "sell rating": 2.0, "loss": 1.5, "losses": 1.5, "misses": 1.8,
    "miss": 1.8, "below estimates": 1.5, "decline": 1.2, "declines": 1.2,
    "falls": 1.0, "slump": 1.5, "plunge": 1.8, "crash": 2.0,
    "weak": 1.2, "weakness": 1.2, "sluggish": 1.2, "concerns": 1.0,
    "pledge": 1.8, "pledged": 1.8, "stake sale": 1.3, "block deal": 0.8,
    "lawsuit": 1.8, "litigation": 1.5, "auditor": 1.5, "qualified opinion": 2.5,
    "delay": 1.2, "delayed": 1.2, "shutdown": 1.8, "layoff": 1.3,
    "margin pressure": 1.5, "margin contraction": 1.5, "debt": 0.6,
    "impairment": 1.8, "writeoff": 2.0, "write-off": 2.0, "recall": 1.8,
}

#: Phrases that invert the term following them. Crude, and the bots say so.
NEGATORS = ("no ", "not ", "never ", "without ", "denies ", "denied ", "rejects ")


# --- Source credibility -----------------------------------------------------
# Tier 1 is the exchange itself: a filing is a fact, not a report of one.

SOURCE_TIERS: dict[str, int] = {
    "nseindia.com": 1, "bseindia.com": 1, "sebi.gov.in": 1, "rbi.org.in": 1,
    "reuters.com": 2, "bloomberg.com": 2, "economictimes.indiatimes.com": 2,
    "business-standard.com": 2, "livemint.com": 2, "thehindubusinessline.com": 2,
    "moneycontrol.com": 3, "cnbctv18.com": 3, "financialexpress.com": 3,
    "businesstoday.in": 3, "ndtvprofit.com": 3, "zeebiz.com": 3,
}
DEFAULT_TIER = 4

TIER_WEIGHT = {1: 1.0, 2: 0.85, 3: 0.65, 4: 0.35}

TIER_LABEL = {
    1: "exchange or regulator filing",
    2: "established financial newswire",
    3: "mainstream business media",
    4: "unranked source",
}


# --- Event taxonomy ---------------------------------------------------------
# Materiality is what separates a genuine re-rating from a press release.
# Each entry: patterns, direction, materiality 0-1.

EVENT_TYPES: dict[str, dict[str, Any]] = {
    # "Quarter" alone is far too loose - it appears in shareholding patterns,
    # investor complaint statements and half the compliance calendar. The
    # word has to sit next to something that means earnings.
    "results": {
        "patterns": (r"financial results?", r"(un)?audited results?",
                     r"results? for the (quarter|year|half)", r"quarterly results?",
                     r"\bearnings\b", r"\bq[1-4]\s?(fy)?\d*\s+results?",
                     r"statement of.*financial results?"),
        "direction": 0, "materiality": 0.9,
        "note": "scheduled and priced in ahead; the surprise matters, not the event",
    },
    "rating_action": {
        "patterns": (r"\bupgrade", r"\bdowngrade", r"rating", r"\boutlook\b", r"credit rating"),
        "direction": 0, "materiality": 0.7,
        "note": "a third party restating a view, often after the fact",
    },
    "order_win": {
        "patterns": (r"order win", r"\bbags\b", r"\bwins\b", r"\bcontract\b", r"\blou\b", r"letter of award"),
        "direction": 1, "materiality": 0.6,
        "note": "revenue visibility, though size against order book is what counts",
    },
    "capacity": {
        "patterns": (r"capacity", r"expansion", r"new plant", r"commission", r"greenfield", r"brownfield"),
        "direction": 1, "materiality": 0.6,
    },
    "capital_raise": {
        "patterns": (r"\bqip\b", r"preferential", r"rights issue", r"fund rais", r"\bfpo\b", r"debenture"),
        "direction": -1, "materiality": 0.7,
        "note": "dilution, unless it is funding growth already contracted",
    },
    "pledge": {
        "patterns": (r"pledg", r"encumbr", r"invoke"),
        "direction": -1, "materiality": 0.95,
        "note": "promoter pledging is among the most reliable early warnings",
    },
    # Bare "SEBI" is not a governance event. Every listed company files
    # compliance certificates *under* SEBI regulations every quarter, and
    # matching the regulator's name alone turned mandatory paperwork into a
    # red flag on every stock - which both inflated the flag count and biased
    # the news desk negative across the whole universe. The patterns below
    # require an adverse action, not a mention.
    "governance": {
        "patterns": (r"resignation of .*(auditor|director)", r"auditor.*resign",
                     r"whistle", r"\bfraud\b", r"forensic audit",
                     r"sebi.*(order|penalt|show cause|adjudicat|investigat|summon|debar)",
                     r"(order|penalt|show cause|investigat).*\bsebi\b",
                     r"show cause", r"qualified opinion", r"adverse opinion",
                     r"disqualification", r"insider trading violation"),
        "direction": -1, "materiality": 1.0,
        "note": "governance events are where permanent capital loss comes from",
    },
    # An auditor changing is not the same as an auditor walking out. Rotation
    # is mandatory under the Companies Act and happens on a schedule.
    "auditor_change": {
        "patterns": (r"change in auditor", r"appointment of .*auditor",
                     r"auditor.*appoint", r"re-?appointment of.*auditor"),
        "direction": 0, "materiality": 0.45,
        "note": "could be mandatory rotation or a disagreement - the filing itself says which",
    },
    "litigation": {
        "patterns": (r"lawsuit", r"litigation", r"tribunal", r"\bnclt\b", r"court", r"arbitration", r"penalty"),
        "direction": -1, "materiality": 0.8,
    },
    "ownership": {
        "patterns": (r"stake", r"acquisition", r"acquire", r"merger", r"open offer", r"divest"),
        "direction": 0, "materiality": 0.7,
    },
    "payout": {
        "patterns": (r"dividend", r"buyback", r"bonus issue", r"\bsplit\b"),
        "direction": 1, "materiality": 0.4,
        "note": "cash returned, but not itself evidence the business improved",
    },
    "operational": {
        "patterns": (r"shutdown", r"strike", r"\brecall\b", r"\bfire\b", r"accident", r"disruption"),
        "direction": -1, "materiality": 0.8,
    },
    # Periodic compliance filings. Every listed company makes these on a
    # schedule and they say nothing about the business. Listed explicitly
    # because several of them mention SEBI, auditors or share capital and
    # would otherwise match a serious category.
    "routine": {
        "patterns": (r"intimation", r"disclosure under", r"newspaper publication", r"trading window",
                     r"investor presentation", r"analyst meet", r"schedule of",
                     r"certificate under", r"compliance certificate",
                     r"reconciliation of share capital", r"reg\.? ?\d+", r"regulation \d+",
                     r"corporate governance report", r"shareholding pattern",
                     r"record date", r"book closure", r"annual report",
                     r"postal ballot", r"\bagm\b", r"\begm\b",
                     r"certificate.*regulations", r"statement of investor complaints",
                     r"related party transaction.*disclosure"),
        "direction": 0, "materiality": 0.1,
        "note": "compliance noise; carries no information about the business",
    },
}


# --- Sector sensitivity -----------------------------------------------------
# How a sector responds to each macro variable. Positive means the sector
# benefits when that variable rises.

SECTOR_MACRO_SENSITIVITY: dict[str, dict[str, float]] = {
    "Oil & Gas":              {"crude_brent": 0.5, "usdinr": -0.3},
    "Energy":                 {"crude_brent": 0.4, "usdinr": -0.3},
    "Information Technology": {"usdinr": 0.7, "us_10y": -0.3},
    "Automobile and Auto Components": {"crude_brent": -0.5, "usdinr": -0.3},
    "Chemicals":              {"crude_brent": -0.6, "usdinr": 0.2},
    "Fast Moving Consumer Goods": {"crude_brent": -0.4},
    "Metals & Mining":        {"usdinr": 0.3, "crude_brent": 0.2},
    "Financial Services":     {"us_10y": -0.3, "india_vix": -0.4},
    "Healthcare":             {"usdinr": 0.5},
    "Capital Goods":          {"crude_brent": -0.2},
    "Construction Materials": {"crude_brent": -0.5},
    "Services":               {"usdinr": 0.3},
}


def sentiment_score(text: str) -> tuple[float, list[str]]:
    """Weighted lexicon score for one headline, and the terms that drove it.

    Returns roughly -5..+5. Negation handling is a simple lookbehind, which
    catches "no fraud" but not "the allegations of fraud were dismissed" -
    a limitation the Sentiment Analyst reports rather than hides.
    """
    if not text:
        return 0.0, []

    lowered = " " + str(text).lower() + " "
    total = 0.0
    matched: list[str] = []

    for term, weight in POSITIVE_TERMS.items():
        if term in lowered:
            negated = any(neg + term in lowered for neg in NEGATORS)
            total += -weight if negated else weight
            matched.append(f"{'not ' if negated else ''}{term}")

    for term, weight in NEGATIVE_TERMS.items():
        if term in lowered:
            negated = any(neg + term in lowered for neg in NEGATORS)
            total += weight if negated else -weight
            matched.append(f"{'not ' if negated else ''}{term}")

    return max(-5.0, min(5.0, total)), matched


def classify_event(text: str) -> tuple[str, dict[str, Any]]:
    """Match a headline to the event taxonomy.

    Returns the most material matching type, so that a filing mentioning both
    a routine intimation and an auditor resignation is classified on the
    latter.
    """
    if not text:
        return "unknown", {"direction": 0, "materiality": 0.0}

    lowered = str(text).lower()
    best_name, best_spec, best_materiality = "unknown", {"direction": 0, "materiality": 0.0}, -1.0

    for name, spec in EVENT_TYPES.items():
        if any(re.search(pattern, lowered) for pattern in spec["patterns"]):
            if spec["materiality"] > best_materiality:
                best_name, best_spec, best_materiality = name, spec, spec["materiality"]

    return best_name, best_spec


def source_tier(source: str | None) -> int:
    if not source:
        return DEFAULT_TIER
    lowered = str(source).lower()
    for domain, tier in SOURCE_TIERS.items():
        if domain in lowered:
            return tier
    return DEFAULT_TIER
