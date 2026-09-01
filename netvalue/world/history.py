"""The estimator's training split — observed outcomes, no causes.

This module is what makes the whole submission non-circular.

The agent needs to know how likely a retry is to work. It must not get that by reading
``world/recovery.py``, because then the value engine and the simulator would share one
model of the physics and the agent would win by construction. Instead it gets a log of
what a *previous, unintelligent* system actually did and what happened — the same thing a
real payments team would have — and estimates the curves from it.

Two properties are deliberate:

* **No causes are emitted.** Only observable features, the intervention taken, and the
  outcome. ``Transaction.public_fields`` drops ``true_cause`` and the record schema below
  never reintroduces it.
* **The logging policy explores.** A production dunning system that only ever retries
  would leave the estimator blind about contacts and escalations, and the agent could not
  reason about actions it had never seen evidence for. The behavioural policy here is
  randomised across the whole intervention set, which is exactly the "logged under a
  different policy" situation off-policy evaluation exists to handle — and is why that
  sits on the closing slide as work this project scoped but did not do.
"""

from __future__ import annotations

from datetime import timedelta

from pydantic import BaseModel, ConfigDict

from netvalue.world import rng
from netvalue.world.banks import WorldHealth
from netvalue.world.calendar import days_to_nearest_salary_day
from netvalue.world.config import ErrorCode, Intervention, Rail, Segment, WorldConfig
from netvalue.world.generator import Transaction, generate_transactions
from netvalue.world.recovery import (
    RecoveryContext,
    card_update_succeeds,
    debit_succeeds,
    escalation_succeeds,
)

#: Interventions the historical system was capable of, and how often it reached for each.
#: Weighted toward retrying, because that is what unintelligent systems actually do, but
#: with enough mass elsewhere to give the estimator coverage. [chosen]
_LOGGING_POLICY: dict[Intervention, float] = {
    Intervention.RETRY_NOW: 0.34,
    Intervention.RETRY_AFTER: 0.26,
    Intervention.SWITCH_ROUTE_AND_RETRY: 0.10,
    Intervention.REQUEST_CARD_UPDATE: 0.20,
    Intervention.ESCALATE_TO_HUMAN: 0.10,
}

_RETRY_DELAYS_H: tuple[float, ...] = (24.0, 36.0, 48.0, 72.0, 120.0)


class HistoryRecord(BaseModel):
    """One logged action and its outcome. Contains no ground truth of any kind."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    transaction_id: str
    rail: Rail
    error_code: ErrorCode
    amount_inr: float
    segment_label: Segment
    bank_id: str

    attempt_index: int
    contact_index: int
    hours_since_first_failure: float
    days_to_salary: int
    avg_days_late: float
    prior_contact_responses: int
    prior_contacts_sent: int
    card_expiry_visibly_past: bool

    intervention: Intervention
    succeeded: bool


def _expiry_visibly_past(txn: Transaction) -> bool:
    if txn.card_exp_year is None or txn.card_exp_month is None:
        return False
    ref = txn.first_failure_at
    return (txn.card_exp_year, txn.card_exp_month) < (ref.year, ref.month)


def _admissible(txn: Transaction, intervention: Intervention) -> bool:
    if intervention in {
        Intervention.SWITCH_ROUTE_AND_RETRY,
        Intervention.REQUEST_CARD_UPDATE,
    }:
        return txn.rail is Rail.CARD_MANDATE
    return True


def simulate_history(cfg: WorldConfig, health: WorldHealth) -> list[HistoryRecord]:
    """Replay a randomised behavioural policy over a dedicated population.

    The population is generated from a *different* seed than ``dataset_a``, so nothing the
    estimator trains on also appears in the evaluation set.
    """
    hist_cfg = cfg.model_copy(
        update={
            "name": "history",
            "seed": rng.derive_seed(cfg.seed, "history-population") % (2**31),
            "n_transactions": cfg.n_history_transactions,
        }
    )
    population = generate_transactions(hist_cfg)

    records: list[HistoryRecord] = []
    for txn in population:
        contacts = 0
        clock = txn.first_failure_at
        n_actions = 1 + int(
            rng.stream(hist_cfg.seed, "hist-depth", txn.transaction_id).poisson(1.6)
        )

        for attempt in range(1, n_actions + 1):
            options = [i for i in _LOGGING_POLICY if _admissible(txn, i)]
            weights = [_LOGGING_POLICY[i] for i in options]
            action = rng.choice(
                hist_cfg.seed, options, weights, "hist-action", txn.transaction_id, attempt
            )

            # The regulatory floor applies to the historical system too: no debit may be
            # presented inside the 24h pre-debit notification window.
            if action is Intervention.RETRY_AFTER:
                delay = rng.choice(
                    hist_cfg.seed,
                    list(_RETRY_DELAYS_H),
                    [1.0] * len(_RETRY_DELAYS_H),
                    "hist-delay",
                    txn.transaction_id,
                    attempt,
                )
            else:
                delay = cfg.bounds.min_inter_attempt_hours
            clock = clock + timedelta(hours=float(delay))
            if clock >= txn.expires_at:
                break

            is_contact = cfg.costs.interventions[action].consumes_contact
            if is_contact:
                contacts += 1
                if contacts > cfg.bounds.max_contacts_per_transaction:
                    break

            ctx = RecoveryContext(
                transaction_id=txn.transaction_id,
                true_cause=txn.true_cause,
                rail=txn.rail,
                amount_inr=txn.amount_inr,
                segment=txn.segment,
                bank_id=txn.bank_id,
                route=txn.acquirer_route,
                when=clock,
                attempt_index=attempt,
                contact_index=max(1, contacts),
                health=health,
                route_switched=action is Intervention.SWITCH_ROUTE_AND_RETRY,
            )

            if action is Intervention.REQUEST_CARD_UPDATE:
                succeeded = card_update_succeeds(hist_cfg, ctx)
            elif action is Intervention.ESCALATE_TO_HUMAN:
                succeeded = escalation_succeeds(hist_cfg, ctx)
            else:
                succeeded = debit_succeeds(hist_cfg, ctx, action)

            records.append(
                HistoryRecord(
                    transaction_id=txn.transaction_id,
                    rail=txn.rail,
                    error_code=txn.error_code,
                    amount_inr=txn.amount_inr,
                    segment_label=txn.segment,
                    bank_id=txn.bank_id,
                    attempt_index=attempt,
                    contact_index=max(1, contacts),
                    hours_since_first_failure=round(
                        (clock - txn.first_failure_at).total_seconds() / 3600.0, 2
                    ),
                    days_to_salary=days_to_nearest_salary_day(clock),
                    avg_days_late=txn.customer_history.avg_days_late,
                    prior_contact_responses=txn.customer_history.prior_contact_responses,
                    prior_contacts_sent=txn.customer_history.prior_contacts_sent,
                    card_expiry_visibly_past=_expiry_visibly_past(txn),
                    intervention=action,
                    succeeded=succeeded,
                )
            )

            if succeeded:
                break

    return records
