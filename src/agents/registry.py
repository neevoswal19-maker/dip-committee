"""The org chart, in one place.

Wave order is not cosmetic. The review desk reads the other analysts'
verdicts, so it has to run after them - Bull and Bear argue from what the
committee found, not from raw data.
"""

from __future__ import annotations

from typing import Any, Iterable

from src.agents import equity_desk, macro_desk, news_desk, ownership_desk, review_desk
from src.agents.base import Analyst

#: Desks that read raw evidence and can run in any order.
PRIMARY_DESKS: dict[str, tuple[type[Analyst], ...]] = {
    "news": news_desk.ANALYSTS,
    "equity": equity_desk.ANALYSTS,
    "macro": macro_desk.ANALYSTS,
    "ownership": ownership_desk.ANALYSTS,
}

#: Runs last: these read the primary desks' verdicts.
REVIEW_DESK: tuple[type[Analyst], ...] = review_desk.ANALYSTS

DESK_ORDER = ("news", "equity", "macro", "ownership", "review")


def all_analyst_classes() -> list[type[Analyst]]:
    classes: list[type[Analyst]] = []
    for desk in PRIMARY_DESKS.values():
        classes.extend(desk)
    classes.extend(REVIEW_DESK)
    return classes


def build(cfg: Any = None) -> dict[str, list[Analyst]]:
    """Instantiate every bot, grouped by desk."""
    desks: dict[str, list[Analyst]] = {
        desk: [cls(cfg) for cls in classes] for desk, classes in PRIMARY_DESKS.items()
    }
    desks["review"] = [cls(cfg) for cls in REVIEW_DESK]
    return desks


def bot_count() -> int:
    """17 analysts + 5 leads + 1 CMIO."""
    return len(all_analyst_classes()) + len(DESK_ORDER) + 1


def describe() -> list[dict[str, str]]:
    """The chart as data, for the dashboard to render."""
    rows: list[dict[str, str]] = []
    for desk in DESK_ORDER:
        classes = PRIMARY_DESKS.get(desk, REVIEW_DESK if desk == "review" else ())
        for cls in classes:
            rows.append({"desk": desk, "bot_id": cls.bot_id, "name": cls.name, "role": "analyst"})
    return rows
