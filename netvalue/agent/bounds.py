"""The deterministic gate layer.

Two rules govern this module, and both are about what it is *not* allowed to do.

**It can only shrink the admissible set, never enlarge it.** A gate removes actions; it
never adds one, never substitutes a cheaper one, never overrides the engine's ranking among
what survives. That keeps the separation clean: the value engine decides what is *worth*
doing, the gates decide what is *permitted*, and neither can quietly become the other.

**A model may not enforce a cap.** Every constraint here is arithmetic on integers and
timestamps. The economics are already model-adjacent — they consume a learned probability
and a diagnosed belief — so the constraints that keep the agent lawful and bounded must not
be. Bounds you cannot verify are not bounds.

Every gate that fires is recorded with a name, and that name reaches the audit log. A
policy blocked by ``mandate_cycle_cap`` and one that chose to stop are the same action and
completely different decisions, and the log has to tell them apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from netvalue.agent.belief import Belief
from netvalue.agent.diagnose.schema import DiagnosedCause
from netvalue.agent.economics import CONTACT_ACTIONS, DEBIT_ACTIONS
from netvalue.agent.observation import Observation, ObservedRail
from netvalue.agent.policy import ActionKind
from netvalue.agent.value import Candidate


@dataclass(slots=True)
class BoundsConfig:
    """Hard limits. Regulatory ones are marked; the rest are merchant policy."""

    max_attempts_per_transaction: int = 6
    max_contacts_per_transaction: int = 3
    #: Merchant policy, not a network rule — India caps no number of retries per cycle.
    max_debits_per_mandate_cycle: int = 3
    #: Regulatory: every debit needs a fresh pre-debit notification at least this far
    #: ahead, which is why ``retry_now`` is inexpressible on these rails.
    min_inter_attempt_hours: float = 24.0
    #: Once the belief concentrates here, further plain retries cannot work by definition.
    card_expired_belief_threshold: float = 0.50
    card_expired_retry_limit: int = 1


@dataclass(slots=True)
class GateResult:
    """What survived, and what removed the rest."""

    allowed: list[Candidate]
    fired: list[str] = field(default_factory=list)

    @property
    def gate_names(self) -> str:
        return ",".join(self.fired) if self.fired else ""


def apply(
    candidates: list[Candidate],
    observation: Observation,
    belief: Belief,
    *,
    config: BoundsConfig | None = None,
    now: datetime | None = None,
    last_debit_at: datetime | None = None,
) -> GateResult:
    """Filter to the permitted actions, naming every gate that removed something.

    ``abandon`` is never filtered. It is always lawful, which is what guarantees the agent
    can always decline to act rather than being forced into a spend by an empty action set.
    """
    cfg = config or BoundsConfig()
    when = now or observation.observed_at
    attempts_used = observation.attempt_number - 1
    contacts_used = observation.contacts_used
    fired: list[str] = []
    allowed: list[Candidate] = []

    debit_budget = min(cfg.max_attempts_per_transaction, cfg.max_debits_per_mandate_cycle)
    card_expired_mass = belief.probability(DiagnosedCause.CARD_EXPIRED)

    for candidate in candidates:
        kind = candidate.action.kind
        if kind is ActionKind.ABANDON:
            allowed.append(candidate)
            continue

        blocked: str | None = None

        if kind in DEBIT_ACTIONS:
            if attempts_used >= cfg.max_attempts_per_transaction:
                blocked = "max_attempts"
            elif attempts_used >= debit_budget:
                blocked = "mandate_cycle_cap"
            elif (
                card_expired_mass > cfg.card_expired_belief_threshold
                and attempts_used >= cfg.card_expired_retry_limit
                and kind is not ActionKind.SWITCH_ROUTE_AND_RETRY
            ):
                # A retry cannot fix an expired card. Once the belief says that is most
                # likely, further plain retries are spending to learn something already
                # known — so this is a hard block, not a preference the engine may outbid.
                blocked = "card_expired_retry_block"
            else:
                landing = _landing_time(candidate, when)
                if landing >= observation.expires_at:
                    blocked = "past_expiry"
                elif last_debit_at is not None and landing < last_debit_at + timedelta(
                    hours=cfg.min_inter_attempt_hours
                ):
                    blocked = "min_inter_attempt"

        elif kind in CONTACT_ACTIONS:
            if contacts_used >= cfg.max_contacts_per_transaction:
                blocked = "max_contacts"
            elif (
                kind is ActionKind.REQUEST_CARD_UPDATE
                and observation.rail is not ObservedRail.CARD_MANDATE
            ):
                blocked = "rail_unsupported"

        if blocked is None:
            allowed.append(candidate)
        elif blocked not in fired:
            fired.append(blocked)

    if not allowed:  # pragma: no cover - abandon is always present
        raise AssertionError("the gate layer removed every action, including abandon")
    return GateResult(allowed=allowed, fired=fired)


def _landing_time(candidate: Candidate, now: datetime) -> datetime:
    action = candidate.action
    if action.scheduled_at is not None:
        return action.scheduled_at
    return now + timedelta(hours=action.delay_hours or 0.0)
