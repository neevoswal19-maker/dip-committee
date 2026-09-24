"""Suggested rule changes - "how could we have won more" - for the owner to decide.

`postmortem` replays each closed trade with alternative exits. This module
looks for a pattern across them: if one alternative would have beaten what
was actually done, clearly and in most trades, it becomes a proposal.

It never applies anything. Changing an exit rule changes what the strategy
is (`learning.allow_threshold_auto_apply: false`). A proposal waits on the
dashboard; if the owner approves it, the weekly job opens a pull request
that edits config.yaml, and nothing changes until that is merged.

The bar is deliberately high, across winners *and* losers - a looser
trailing stop that rescues a few winners but rides every loser further down
is not an improvement:

  at least MIN_TRADES reviewed trades,
  the alternative beats the actual exit by MIN_EDGE_POINTS on average, and
  it does so in at least MIN_SHARE of trades.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select

from src import db

log = logging.getLogger(__name__)

MIN_TRADES = 8
MIN_EDGE_POINTS = 2.0
MIN_SHARE = 0.60

#: Which alternative maps to which config value.
CANDIDATES = {
    "10% trailing stop": ("exit.trailing_stop.trail_pct", 10.0),
    "20% trailing stop": ("exit.trailing_stop.trail_pct", 20.0),
}


def reviewed_trades() -> list[dict[str, Any]]:
    """One evidence record per reviewed trade."""
    with db.connection() as conn:
        rows = conn.execute(select(db.lessons.c.trade_id, db.lessons.c.evidence)
                            .where(db.lessons.c.trade_id.isnot(None))).fetchall()
    seen: dict[int, dict[str, Any]] = {}
    for row in rows:
        if row.trade_id not in seen:
            seen[row.trade_id] = db.from_json(row.evidence, {})
    return list(seen.values())


def find(cfg: Any, evidence: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    trades = evidence if evidence is not None else reviewed_trades()
    if len(trades) < MIN_TRADES:
        return []

    found = []
    for name, (path, value) in CANDIDATES.items():
        current = cfg.get(path)
        if current is not None and float(current) == value:
            continue          # already the rule
        pairs = [(t["return_pct"], t["alternatives"][name]) for t in trades
                 if name in (t.get("alternatives") or {})]
        if len(pairs) < MIN_TRADES:
            continue
        gains = [alt - actual for actual, alt in pairs]
        edge = sum(gains) / len(gains)
        share = sum(1 for g in gains if g > 0) / len(gains)
        if edge >= MIN_EDGE_POINTS and share >= MIN_SHARE:
            found.append({
                "kind": "rule", "config_path": path,
                "current_value": str(current), "proposed_value": str(value),
                "rationale": (
                    f"Replayed on {len(pairs)} closed trades, a {name} would have made "
                    f"{edge:+.1f} points more on average and done better in {share:.0%} of them."
                ),
                "evidence_json": json.dumps({"trades": len(pairs), "avg_edge_points": round(edge, 2),
                                             "share_better": round(share, 3)}),
            })
    return found


def record(found: list[dict[str, Any]]) -> int:
    """Store new proposals, skipping any already pending for the same setting."""
    if not found:
        return 0
    with db.connection() as conn:
        pending = {(r.config_path, r.proposed_value) for r in conn.execute(
            select(db.proposals.c.config_path, db.proposals.c.proposed_value)
            .where(db.proposals.c.status.in_(("pending", "approved"))))}
        new = [dict(f, created_at=db.now(), status="pending") for f in found
               if (f["config_path"], f["proposed_value"]) not in pending]
        if new:
            conn.execute(db.proposals.insert().values(new))
    return len(new)


def decide(proposal_id: int, approve: bool) -> None:
    with db.connection() as conn:
        conn.execute(db.proposals.update().where(db.proposals.c.id == proposal_id)
                     .values(status="approved" if approve else "rejected", decided_at=db.now()))


def listing(status: str | None = None) -> list[dict[str, Any]]:
    query = select(db.proposals).order_by(db.proposals.c.id.desc())
    if status:
        query = query.where(db.proposals.c.status == status)
    with db.connection() as conn:
        return [dict(r._mapping) for r in conn.execute(query).fetchall()]


# --- Applying an approved change: edit config.yaml in place --------------------------


def set_scalar(text: str, dotted_path: str, value: str) -> str:
    """Change one scalar in YAML text, keeping every comment and all layout.

    A YAML library would rewrite the file and drop the comments, which hold
    the evidence for every number in it. This walks the indentation instead:
    each key must be found inside the block of the one before it.
    """
    keys = dotted_path.split(".")
    lines = text.split("\n")
    start, end, indent = 0, len(lines), -1

    def content(i: int) -> tuple[str, int] | None:
        stripped = lines[i].lstrip(" ")
        if not stripped or stripped.startswith("#"):
            return None
        return stripped, len(lines[i]) - len(stripped)

    for depth, key in enumerate(keys):
        # A key only counts at the depth of its parent's direct children -
        # never a same-named key nested further in, or in another section.
        child_level = next((c[1] for i in range(start, end) if (c := content(i))), None)
        found = None
        for i in range(start, end):
            c = content(i)
            if c is None:
                continue
            stripped, level = c
            if level <= indent:
                break
            if level == child_level and stripped.startswith(f"{key}:"):
                found = (i, level)
                break
        if found is None:
            raise KeyError(f"{dotted_path}: '{key}' not found")
        i, level = found
        if depth == len(keys) - 1:
            line = lines[i]
            head, _, rest = line.partition(":")
            comment = ""
            if "#" in rest:
                body, _, note = rest.partition("#")
                comment = " " * max(1, len(body) - len(body.rstrip()) or 1) + "#" + note
            lines[i] = f"{head}: {value}{comment}"
            return "\n".join(lines)
        # Narrow to this key's block.
        start, indent = i + 1, level
        for j in range(i + 1, len(lines)):
            s = lines[j].lstrip(" ")
            if s and not s.startswith("#") and len(lines[j]) - len(s) <= level:
                end = j
                break
        else:
            end = len(lines)
    raise KeyError(dotted_path)
