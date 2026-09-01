"""Baselines 3 and 3b — the success-rate ceiling. **This is the row that matters.**

Razorpay's core competency is payment success rate. These two policies are what
maximising success rate looks like when cost is not a consideration, which makes them the
honest embodiment of the target this project argues is the wrong one.

The expected result — and the entire submission — is one line of a table:

    the net-value agent recovers LESS MONEY than these, and produces MORE NET VALUE.

Reported side by side, with confidence intervals, so the trade is explicit rather than
asserted.

**These are oracles by construction and that is the point.** They are handed ground truth
so that they represent a genuine ceiling rather than a strawman: if the agent beat a
*weak* max-recovery policy the comparison would prove nothing. This module is therefore the
sole entry on the boundary allowlist in ``tests/test_boundary.py``.

Ground truth arrives by explicit injection, never through the ``Policy`` protocol. The
protocol stays identical for every policy, so the harness cannot tell an oracle from the
agent and cannot accidentally advantage either.
"""

from __future__ import annotations

from datetime import timedelta

from netvalue.agent.observation import Observation
from netvalue.agent.policy import Action, ActionKind
from netvalue.world.calendar import SALARY_DAYS
from netvalue.world.config import Cause, Rail, WorldConfig
from netvalue.world.generator import Transaction


def _next_salary_day(observation: Observation) -> object:
    """The next salary credit at or after now, capped at the mandate horizon."""
    now = observation.observed_at
    best = None
    for offset in range(0, 40):
        day = now + timedelta(days=offset)
        if day.day in SALARY_DAYS and day >= now:
            best = day.replace(hour=9, minute=0, second=0, microsecond=0)
            break
    if best is None or best >= observation.expires_at:
        return None
    return best


class MaxRecoveryPolicy:
    """Baseline 3 — pursue everything recoverable, ignoring cost.

    Knows *which* transactions can be recovered but not *why* they failed, so it works a
    generic escalation ladder: retry until the attempt cap, then contact, then escalate.
    That is exactly how a success-rate-maximising system behaves when it has good coverage
    data and no cost model — it spends every lever it has on every asset that has a pulse.
    """

    name = "max_recovery"

    def __init__(self, cfg: WorldConfig, truth: dict[str, Transaction]) -> None:
        self.cfg = cfg
        self.truth = truth

    def _unrecoverable(self, txn: Transaction) -> bool:
        return self.cfg.causes[txn.true_cause].permanently_unrecoverable

    def decide(self, observation: Observation) -> Action:
        txn = self.truth[observation.transaction_id]
        if self._unrecoverable(txn):
            # Perfect knowledge of recoverability: it does not waste effort on dead
            # mandates. Withholding this would make the ceiling artificially easy to beat.
            return Action(kind=ActionKind.ABANDON, rationale="oracle: unrecoverable")

        bounds = self.cfg.bounds
        # The binding constraint on debits is whichever cap is tighter. Ignoring that was
        # a real bug: the episode terminated on the cycle cap before this policy ever
        # reached a customer contact, so the "max-recovery" ceiling made zero contacts,
        # incurred zero annoyance cost, and was not maximising recovery at all.
        debit_budget = min(
            bounds.max_attempts_per_transaction, bounds.max_debits_per_mandate_cycle
        )
        if observation.attempt_number <= debit_budget:
            if (
                observation.is_card
                and observation.attempt_number == 2
            ):
                return Action(
                    kind=ActionKind.SWITCH_ROUTE_AND_RETRY,
                    delay_hours=bounds.min_inter_attempt_hours,
                    rationale="max-recovery: try the other route",
                )
            return Action(
                kind=ActionKind.RETRY_AFTER,
                delay_hours=bounds.min_inter_attempt_hours,
                rationale="max-recovery: retry regardless of cost",
            )

        if observation.is_card and observation.contacts_used < bounds.max_contacts_per_transaction:
            return Action(
                kind=ActionKind.REQUEST_CARD_UPDATE,
                rationale="max-recovery: ask the customer, cost ignored",
            )
        if observation.contacts_used < bounds.max_contacts_per_transaction:
            return Action(
                kind=ActionKind.ESCALATE_TO_HUMAN,
                rationale="max-recovery: escalate, cost ignored",
            )
        return Action(kind=ActionKind.ABANDON, rationale="max-recovery: levers exhausted")

    def reset(self) -> None:
        return None


class MaxRecoveryOraclePolicy:
    """Baseline 3b — the true ceiling. Knows the cause and plays the ideal lever first.

    Nothing can recover more than this without changing the physics, so it bounds how much
    of the gap to max-recovery is attributable to *diagnosis* rather than to *economics*.
    Phase 8's ablation reads the difference between this and Baseline 3 as the headroom a
    perfect diagnoser would buy.
    """

    name = "max_recovery_oracle"

    def __init__(self, cfg: WorldConfig, truth: dict[str, Transaction]) -> None:
        self.cfg = cfg
        self.truth = truth

    def decide(self, observation: Observation) -> Action:
        txn = self.truth[observation.transaction_id]
        cause = txn.true_cause
        bounds = self.cfg.bounds
        floor = bounds.min_inter_attempt_hours

        if self.cfg.causes[cause].permanently_unrecoverable:
            return Action(kind=ActionKind.ABANDON, rationale="oracle: mandate is dead")

        debit_budget = min(
            bounds.max_attempts_per_transaction, bounds.max_debits_per_mandate_cycle
        )
        debits_exhausted = observation.attempt_number > debit_budget

        # Causes that only a customer contact or a human can resolve are handled below
        # regardless of the debit budget; retry-recoverable causes have nothing left once
        # the budget is gone, so stopping there avoids firing a gate to learn it.
        if debits_exhausted and cause not in {Cause.CARD_EXPIRED, Cause.RISK_BLOCK}:
            return Action(kind=ActionKind.ABANDON, rationale="oracle: debit budget spent")

        match cause:
            case Cause.CARD_EXPIRED:
                if observation.contacts_used < bounds.max_contacts_per_transaction:
                    return Action(
                        kind=ActionKind.REQUEST_CARD_UPDATE,
                        rationale="oracle: only a card update can work",
                    )
                return Action(kind=ActionKind.ABANDON, rationale="oracle: contacts exhausted")

            case Cause.RISK_BLOCK:
                if observation.contacts_used < bounds.max_contacts_per_transaction:
                    return Action(
                        kind=ActionKind.ESCALATE_TO_HUMAN,
                        rationale="oracle: only a human can lift a risk block",
                    )
                return Action(kind=ActionKind.ABANDON, rationale="oracle: contacts exhausted")

            case Cause.ROUTE_DEGRADED:
                return Action(
                    kind=ActionKind.SWITCH_ROUTE_AND_RETRY,
                    delay_hours=floor,
                    rationale="oracle: the route is degraded, switch it",
                )

            case Cause.INSUFFICIENT_FUNDS:
                target = _next_salary_day(observation)
                if target is not None:
                    return Action(
                        kind=ActionKind.SCHEDULE_RETRY_AT,
                        scheduled_at=target,  # type: ignore[arg-type]
                        rationale="oracle: wait for the salary credit",
                    )
                return Action(
                    kind=ActionKind.RETRY_AFTER, delay_hours=floor,
                    rationale="oracle: no salary day left inside the horizon",
                )

            case Cause.BANK_OUTAGE:
                return Action(
                    kind=ActionKind.RETRY_AFTER,
                    delay_hours=max(floor, 24.0),
                    rationale="oracle: wait out the outage",
                )

            case _:
                return Action(
                    kind=ActionKind.RETRY_AFTER, delay_hours=floor,
                    rationale="oracle: retry is the best available lever",
                )

    def reset(self) -> None:
        return None


def build_truth(txns: list[Transaction]) -> dict[str, Transaction]:
    """Ground-truth lookup for the oracle baselines. Never given to the agent."""
    return {t.transaction_id: t for t in txns}


def is_card(txn: Transaction) -> bool:
    return txn.rail is Rail.CARD_MANDATE
