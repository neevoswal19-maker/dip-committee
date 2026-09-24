"""The desk-weight learner: the one part of the system that changes itself.

It adjusts how much each of the five desks counts toward conviction, from
how well each desk's verdicts have ranked outcomes. Because it acts without
asking, every guardrail in `config.yaml` (learning:) is enforced here:

* **Minimum evidence.** Nothing moves before `min_observations` committee
  runs have a matured outcome at the primary horizon.
* **Shrinkage.** A desk's suggested change is pulled most of the way back
  toward its current weight (`shrinkage` 0.7 keeps 70% of the old weight's
  say). Small samples produce loud, unreliable ICs; this is the damping.
* **A step limit.** No weight moves by more than `max_weight_step_pct` of
  its value in one update.
* **Out-of-sample gate.** The runs are split by date; the change is worked
  out on the older 70% and kept only if it also ranks the *most recent* 30%
  better than the current weights do. A change that only fits the past it
  was fitted on is recorded and not applied.
* **Every version is kept**, with its reason, and any earlier one can be
  restored.

Rule thresholds (stops, targets, entry levels) are never touched here; they
change the strategy itself and go through `proposals` for the owner's click.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from src import db
from src.learning import attribution

log = logging.getLogger(__name__)

#: How far a desk's IC moves its target: an IC of 0.10 suggests +50% before
#: shrinkage and the step limit cut it down.
IC_SCALE = 5.0


def config_weights(cfg: Any) -> dict[str, float]:
    return {k: float(v) for k, v in (cfg.get("committee.desk_weights", {}) or {}).items()}


def active_weights(cfg: Any) -> dict[str, float]:
    """The weights the committee should use now: the learner's, else config's."""
    try:
        payload = db.active_weights()
    except Exception as exc:  # no database, or it is unreachable
        log.debug("Active weights unavailable: %s", exc)
        payload = None
    weights = (payload or {}).get("desk_weights")
    if not weights:
        return config_weights(cfg)
    return {k: float(v) for k, v in weights.items()}


def seed(cfg: Any) -> int | None:
    """Record the config weights as version one, if nothing is active yet."""
    if db.active_weights():
        return None
    with db.connection() as conn:
        return conn.execute(db.weight_versions.insert().values(
            created_at=db.now(), is_active=True, source="seed",
            payload_json=db.to_json({"desk_weights": config_weights(cfg)}),
            rationale="Starting weights from config.yaml (committee.desk_weights).",
        )).inserted_primary_key[0]


@dataclass
class WeightProposal:
    current: dict[str, float]
    proposed: dict[str, float]
    n_runs: int
    in_sample_ic: dict[str, float | None]
    held_out_ic_current: float | None
    held_out_ic_proposed: float | None
    passed_gate: bool
    rationale: str
    horizon: int
    reasons: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return any(abs(self.proposed[d] - self.current.get(d, 0.0)) > 1e-9 for d in self.proposed)


def run_observations(horizon: int) -> list[dict[str, Any]]:
    """One row per committee run: each desk's score and the run's outcome, oldest first."""
    with db.connection() as conn:
        rows = conn.execute(
            select(db.bot_verdicts.c.run_id, db.bot_verdicts.c.desk, db.bot_verdicts.c.score,
                   db.trade_checkpoints.c.excess_return_pct, db.committee_runs.c.run_at)
            .join(db.trade_checkpoints, db.trade_checkpoints.c.committee_run_id == db.bot_verdicts.c.run_id)
            .join(db.committee_runs, db.committee_runs.c.id == db.bot_verdicts.c.run_id)
            .where(db.bot_verdicts.c.role == "lead")
            .where(db.bot_verdicts.c.data_available.is_(True))
            .where(db.trade_checkpoints.c.horizon_days == horizon)
            .where(db.trade_checkpoints.c.excess_return_pct.isnot(None))
        ).fetchall()

    runs: dict[int, dict[str, Any]] = {}
    for r in rows:
        entry = runs.setdefault(r.run_id, {"run_id": r.run_id, "run_at": r.run_at,
                                           "outcome": float(r.excess_return_pct), "desks": {}})
        if r.score is not None:
            entry["desks"][r.desk] = float(r.score)
    return sorted(runs.values(), key=lambda e: (e["run_at"], e["run_id"]))


def _blend(desks: dict[str, float], weights: dict[str, float]) -> float | None:
    total = sum(weights.get(d, 0.0) for d in desks)
    if total <= 0:
        return None
    return sum(s * weights.get(d, 0.0) for d, s in desks.items()) / total


def _ic(values: list[float | None], outcomes: list[float]) -> float | None:
    pairs = [(v, o) for v, o in zip(values, outcomes) if v is not None]
    if len(pairs) < 10:
        return None
    result = attribution.information_coefficient([p[0] for p in pairs], [p[1] for p in pairs])
    return None if result is None else result.ic


def propose(cfg: Any, observations: list[dict[str, Any]] | None = None) -> WeightProposal | None:
    """Work out a new set of desk weights, or None if the evidence is too thin."""
    horizon = int(cfg.get("learning.primary_horizon_days", 90))
    obs = observations if observations is not None else run_observations(horizon)
    min_n = int(cfg.get("learning.min_observations", 30))
    if len(obs) < min_n:
        return None

    shrink = float(cfg.get("learning.shrinkage", 0.70))
    max_step = float(cfg.get("learning.max_weight_step_pct", 20.0)) / 100.0
    floor_ic = float(cfg.get("learning.min_ic_to_keep", -0.05))
    held_out_share = float(cfg.get("learning.out_of_sample_fraction", 0.30))

    split = int(round(len(obs) * (1 - held_out_share)))
    train, held_out = obs[:split], obs[split:]

    current = active_weights(cfg)
    proposed = dict(current)
    in_sample: dict[str, float | None] = {}
    reasons = []

    for desk, weight in current.items():
        pairs = [(o["desks"][desk], o["outcome"]) for o in train if desk in o["desks"]]
        result = (attribution.information_coefficient([p[0] for p in pairs], [p[1] for p in pairs])
                  if len(pairs) >= 10 else None)
        if result is None:
            in_sample[desk] = None
            reasons.append(f"{desk}: too few scored runs ({len(pairs)}) - unchanged")
            continue
        in_sample[desk] = result.ic
        raw = 1.0 + result.ic * IC_SCALE
        step = (raw - 1.0) * (1.0 - shrink)
        if result.ic < floor_ic and result.p_value <= 0.10:
            step = -max_step            # reliably wrong: cut as far as allowed
        step = max(-max_step, min(max_step, step))
        proposed[desk] = weight * (1.0 + step)
        reasons.append(f"{desk}: IC {result.ic:+.3f} (p {result.p_value:.2f}) -> {step:+.0%}")

    # No rescaling to the old total: the committee divides by the total
    # weight, so only the ratios matter - and rescaling could carry a weight
    # past the step limit it has just been held to.
    proposed = {d: round(w, 4) for d, w in proposed.items()}

    outcomes = [o["outcome"] for o in held_out]
    ic_current = _ic([_blend(o["desks"], current) for o in held_out], outcomes)
    ic_proposed = _ic([_blend(o["desks"], proposed) for o in held_out], outcomes)
    passed = ic_current is not None and ic_proposed is not None and ic_proposed > ic_current

    if ic_current is None or ic_proposed is None:
        gate = f"the held-out slice ({len(held_out)} runs) is too small to test the change"
    elif passed:
        gate = f"it ranked the most recent {len(held_out)} runs better (IC {ic_proposed:+.3f} vs {ic_current:+.3f})"
    else:
        gate = (f"it did not rank the most recent {len(held_out)} runs better "
                f"(IC {ic_proposed:+.3f} vs {ic_current:+.3f}), so it is recorded but not applied")

    return WeightProposal(
        current=current, proposed=proposed, n_runs=len(obs), in_sample_ic=in_sample,
        held_out_ic_current=ic_current, held_out_ic_proposed=ic_proposed, passed_gate=passed,
        rationale=f"From {len(obs)} runs scored at {horizon} days: " + "; ".join(reasons) + f". Gate: {gate}.",
        horizon=horizon, reasons=reasons,
    )


def apply(cfg: Any, proposal: WeightProposal, *, dry_run: bool = False) -> int | None:
    """Record the proposal as a version; make it active only if it passed the gate."""
    if dry_run or not proposal.changed:
        return None
    activate = proposal.passed_gate and bool(cfg.get("learning.auto_apply", True))
    with db.connection() as conn:
        if activate:
            conn.execute(db.weight_versions.update().values(is_active=False))
        return conn.execute(db.weight_versions.insert().values(
            created_at=db.now(), is_active=activate, source="learner",
            payload_json=db.to_json({"desk_weights": proposal.proposed,
                                     "horizon_days": proposal.horizon}),
            n_trades_at_update=proposal.n_runs,
            in_sample_score=None,
            out_of_sample_score=proposal.held_out_ic_proposed,
            passed_oos_gate=proposal.passed_gate,
            rationale=proposal.rationale,
        )).inserted_primary_key[0]


def restore(version_id: int) -> int:
    """Make an earlier version active again, recorded as a new rollback version."""
    with db.connection() as conn:
        target = conn.execute(
            select(db.weight_versions).where(db.weight_versions.c.id == version_id)
        ).first()
        if target is None:
            raise ValueError(f"no weight version {version_id}")
        current = conn.execute(
            select(db.weight_versions.c.id).where(db.weight_versions.c.is_active.is_(True))
        ).first()
        conn.execute(db.weight_versions.update().values(is_active=False))
        return conn.execute(db.weight_versions.insert().values(
            created_at=db.now(), is_active=True, source="rollback",
            payload_json=target.payload_json,
            rationale=f"Restored version {version_id} by hand.",
            rolled_back_from=current.id if current else None,
        )).inserted_primary_key[0]


def history() -> list[dict[str, Any]]:
    with db.connection() as conn:
        rows = conn.execute(select(db.weight_versions).order_by(db.weight_versions.c.id.desc())).fetchall()
    out = []
    for row in rows:
        record = dict(row._mapping)
        record["payload"] = db.from_json(record.pop("payload_json", None), {})
        out.append(record)
    return out
