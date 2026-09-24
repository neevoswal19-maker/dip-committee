"""The weekly learning run.

1. Measure what happened after every committee run whose horizon has come
   due (checkpoints).
2. Score every bot and desk against those outcomes (scorecards).
3. Adjust desk weights if the evidence is strong enough and the change
   survives the out-of-sample gate (weights).
4. Review every newly closed trade, wins included (postmortem).
5. Look for exit rules that would have done better across many trades, and
   file them as proposals for the owner (proposals). Open a pull request for
   any the owner has approved.
6. Send the weekly digest.

`--dry-run` does steps 1 and 2 only, and sends nothing: it writes the
measurements and reports what it would do, without changing any weight or
recording any lesson.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db
from src.alerts import telegram
from src.config import load_config
from src.learning import checkpoints, postmortem, proposals, scorecards, weights

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("learn")


def open_pull_requests(cfg) -> list[str]:
    """Turn each approved proposal into a pull request that edits config.yaml.

    Only on GitHub Actions, where the job's token can push a branch. Nothing
    changes until the owner merges the pull request.
    """
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return []
    opened = []
    for proposal in proposals.listing("approved"):
        branch = f"learner/proposal-{proposal['id']}"
        title = (f"Learner: set {proposal['config_path']} to {proposal['proposed_value']} "
                 f"(approved proposal {proposal['id']})")
        path = Path("config.yaml")
        original = path.read_text(encoding="utf-8")
        try:
            path.write_text(proposals.set_scalar(original, proposal["config_path"],
                                                 proposal["proposed_value"]), encoding="utf-8")
            body = (f"{proposal['rationale']}\n\nApproved on the dashboard. Merging this applies it; "
                    f"closing it leaves the rule as it is.\n\nEvidence: {proposal['evidence_json']}")
            for command in (
                ["git", "checkout", "-b", branch],
                ["git", "-c", "user.name=dip-committee learner",
                 "-c", "user.email=learner@users.noreply.github.com",
                 "commit", "-am", title],
                ["git", "push", "origin", branch],
                ["gh", "pr", "create", "--base", "main", "--head", branch, "--title", title, "--body", body],
            ):
                result = subprocess.run(command, capture_output=True, text=True)
                if result.returncode != 0:
                    raise RuntimeError(f"{' '.join(command[:2])}: {result.stderr.strip()[:300]}")
            url = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else branch
            with db.connection() as conn:
                conn.execute(db.proposals.update().where(db.proposals.c.id == proposal["id"])
                             .values(status="pr_opened"))
            opened.append(url)
        except Exception as exc:
            log.error("Could not open a pull request for proposal %s: %s", proposal["id"], exc)
            telegram.send(
                f"<b>Approved change not applied yet</b>\n{telegram._escape(title)}\n\n"
                f"The pull request could not be opened ({telegram._escape(str(exc)[:200])}). If it says "
                f"Actions may not create pull requests, turn that on under the repository's "
                f"Settings &gt; Actions &gt; General &gt; Workflow permissions.",
                alert_type="learner_error", dedupe_key=f"learner-pr|{proposal['id']}|{date.today()}",
                cfg=cfg,
            )
        finally:
            subprocess.run(["git", "checkout", "-q", "main"], capture_output=True)
            path.write_text(original, encoding="utf-8")
    return opened


def digest(cfg, *, measured: int, cards, proposal, applied_version, reviews, filed: int,
           prs: list[str]) -> str:
    min_n = int(cfg.get("learning.min_observations", 30))
    primary = int(cfg.get("learning.primary_horizon_days", 90))
    maturity = checkpoints.maturity(cfg)
    lines = ["<b>What the committee learned this week</b>",
             f"{measured} new outcome{'s' if measured != 1 else ''} measured. "
             "Outcomes on record: " + ", ".join(f"{h}d {n}" for h, n in sorted(maturity.items())) + "."]

    scored = [c for c in cards if c.horizon_days == primary and c.verdict != "insufficient"]
    if scored:
        best = sorted(scored, key=lambda c: -(c.ic or 0))[:3]
        worst = sorted(scored, key=lambda c: (c.ic or 0))[:2]
        lines.append("Most predictive at %dd: %s." % (primary, ", ".join(
            f"{telegram._escape(c.bot_id)} IC {c.ic:+.2f}" for c in best)))
        lines.append("Least: %s." % ", ".join(f"{telegram._escape(c.bot_id)} IC {c.ic:+.2f}" for c in worst))
    else:
        have = maturity.get(primary, 0)
        lines.append(f"Not enough outcomes yet to judge any bot: {have} of {min_n} needed at {primary} days.")

    if applied_version:
        lines.append("<b>Desk weights changed</b>: " + ", ".join(
            f"{d} {proposal.current[d]:.2f}->{w:.2f}" for d, w in proposal.proposed.items()
            if abs(w - proposal.current.get(d, 0)) > 1e-6) + ". Restore any earlier set on the Learning page.")
    elif proposal is not None:
        lines.append("Desk weights unchanged: " + telegram._escape(proposal.rationale.split("Gate: ")[-1]))

    if reviews:
        lines.append(f"{len(reviews)} closed trade{'s' if len(reviews) != 1 else ''} reviewed.")
    if filed:
        lines.append(f"<b>{filed} rule change{'s' if filed != 1 else ''} proposed</b> - approve or reject on the Learning page.")
    for url in prs:
        lines.append(f"Approved change ready to merge: {telegram._escape(url)}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Weekly learning run")
    parser.add_argument("--dry-run", action="store_true",
                        help="measure and score only; change no weight, record no lesson, send nothing")
    args = parser.parse_args()

    cfg = load_config()
    db.init_db()
    db.assert_encrypted()

    measured = checkpoints.update(cfg)
    cards = scorecards.compute(cfg, write=True)
    log.info("Scorecards: %d (%d with a verdict)", len(cards),
             sum(1 for c in cards if c.verdict != "insufficient"))

    if args.dry_run:
        proposal = weights.propose(cfg)
        log.info("Dry run. Weight proposal: %s", proposal.rationale if proposal else
                 f"insufficient data (need {cfg.get('learning.min_observations', 30)} matured runs)")
        log.info("Unreviewed closed trades: %d", len(postmortem.unreviewed_trades()))
        return 0

    weights.seed(cfg)
    proposal = weights.propose(cfg)
    applied_version = None
    if proposal is not None:
        version = weights.apply(cfg, proposal)
        applied_version = version if (version and proposal.passed_gate) else None
        log.info("Weights: %s", proposal.rationale)
    else:
        log.info("Weights: not enough matured runs yet")

    reviews = postmortem.run(cfg)
    for rv in reviews:
        telegram.send(postmortem.message(rv), alert_type="trade_review", symbol=rv.symbol,
                      dedupe_key=f"review|{rv.trade_id}", cfg=cfg)

    filed = proposals.record(proposals.find(cfg))
    prs = open_pull_requests(cfg)

    week = date.today().isocalendar()
    telegram.send(
        digest(cfg, measured=measured, cards=cards, proposal=proposal,
               applied_version=applied_version, reviews=reviews, filed=filed, prs=prs),
        alert_type="learning_digest", dedupe_key=f"digest|{week[0]}-W{week[1]}", cfg=cfg,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
