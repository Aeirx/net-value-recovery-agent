"""The hidden physics of the world.

**Read this file. You have to defend it.**

``PHASE0_DECISIONS.md`` and the build plan both say this module is the one piece that
cannot be delegated: it is the physics of your world, and every number the project reports
is downstream of it. What follows is grounded in the calibrated record wherever the record
exists, and marked ``[chosen]`` wherever it does not.

**The agent never imports this module.** ``tests/test_boundary.py`` enforces that. The
agent's beliefs about recovery come from ``agent/estimator.py``, fitted on observed
outcomes in ``data/history.jsonl`` — it estimates these curves, it does not read them. If
that boundary ever breaks, the agent wins by construction and every result is a tautology.

Calibration anchors (``CALIBRATION.md`` rows 28-31, secondary grade):

* retry 1 recovers 20-40% of soft declines
* retry 2 recovers a further 15-25% *of the remaining pool*
* retry 3 recovers a further 10-15% of what remains
* beyond attempt 3-4, "rates flatten"

:data:`SOFT_DECLINE_BY_ATTEMPT` encodes exactly that curve. Every retry-recoverable cause
is expressed as a modulation of it rather than as an independently invented number, so the
one externally-anchored shape in the world does as much work as possible.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from netvalue.world import rng
from netvalue.world.banks import WorldHealth
from netvalue.world.calendar import salary_factor
from netvalue.world.config import Cause, Intervention, Rail, Segment, WorldConfig

#: Conditional probability that attempt *k* succeeds, given every earlier attempt failed.
#: Midpoints of the published ranges; index 0 is the first attempt. [sourced, secondary]
SOFT_DECLINE_BY_ATTEMPT: tuple[float, ...] = (0.30, 0.20, 0.125, 0.07)

#: The flat tail beyond the published curve. [chosen]
SOFT_DECLINE_TAIL: float = 0.05

# --- per-cause modulation of the calibrated curve -------------------------------------
# Each is a multiplier on SOFT_DECLINE_BY_ATTEMPT unless stated otherwise. [chosen]

#: A bank outage is near-total while it lasts and clears completely afterwards. The
#: post-outage figure is high because the debit was always going to succeed; the outage
#: only delayed it. This is what makes "wait" a real strategy rather than a stall.
OUTAGE_DURING = 0.02
OUTAGE_AFTER_LIFT = 2.4

#: A degraded route fails on the same route and succeeds on the other one. Switching is
#: cheap, so this cause is mostly a diagnosis problem rather than an economic one.
ROUTE_SAME = 0.06
ROUTE_SWITCHED = 0.72

#: An authorisation that did not complete often completes on a second presentation. But
#: a customer who deliberately opted out of the pre-debit notification tends to opt out
#: again, so the curve decays faster than a plain soft decline.
AFA_LIFT = 1.5
AFA_DECAY = 0.45

#: Retrying into a risk block does essentially nothing. Only escalation moves it.
RISK_BLOCK_RETRY = 0.02


@dataclass(frozen=True, slots=True)
class RecoveryContext:
    """Everything the *world* knows at the moment of an attempt.

    Deliberately a superset of what the agent sees. ``true_cause``, ``health`` and
    ``route`` are exactly the ground truth the agent has to infer.
    """

    transaction_id: str
    true_cause: Cause
    rail: Rail
    amount_inr: float
    segment: Segment
    bank_id: str
    route: str | None
    when: datetime
    attempt_index: int  # 1-based
    contact_index: int  # 1-based, contacts already sent + 1
    health: WorldHealth
    route_switched: bool = False

    #: Replication index. Folded into every keyed draw so that a replication is a
    #: different realised world, while remaining identical across policies at the same
    #: index. This is what makes the Phase 4 comparison paired.
    replication: int = 0


def _rep_key(replication: int) -> tuple[object, ...]:
    """Key fragment identifying the replication.

    **Replication 0 is the canonical world** — the one frozen in ``data/`` — so it
    contributes nothing to the key. That keeps the committed datasets byte-identical
    under regeneration while every other replication draws an independent world.
    """
    return () if replication == 0 else ("rep", replication)


def _soft_decline_base(attempt_index: int) -> float:
    if attempt_index < 1:
        raise ValueError("attempt_index is 1-based")
    if attempt_index <= len(SOFT_DECLINE_BY_ATTEMPT):
        return SOFT_DECLINE_BY_ATTEMPT[attempt_index - 1]
    return SOFT_DECLINE_TAIL


def _clamp(p: float) -> float:
    return max(0.0, min(1.0, p))


def _is_debit_attempt(intervention: Intervention) -> bool:
    """Retry, delayed retry, scheduled retry and a route switch are all *the same
    physical act*: presenting a debit at a moment in time.

    They differ in when they land and on which route, never in what they are. Modelling
    them as one act with different arguments is what makes retry timing a genuine decision
    rather than a menu of differently-named options with hand-tuned success rates.
    """
    return intervention in {
        Intervention.RETRY_NOW,
        Intervention.RETRY_AFTER,
        Intervention.SCHEDULE_RETRY_AT,
        Intervention.SWITCH_ROUTE_AND_RETRY,
    }


def debit_success_probability(
    cfg: WorldConfig, ctx: RecoveryContext, intervention: Intervention
) -> float:
    """``P(this debit attempt succeeds)`` under the true cause. Hidden from the agent."""
    if not _is_debit_attempt(intervention):
        raise ValueError(f"{intervention} is not a debit attempt")

    cause = ctx.true_cause
    base = _soft_decline_base(ctx.attempt_index)

    # Bank identity deliberately does not enter here. A bank's reliability is already
    # expressed in how often and how long it appears in the outage timeline (windows are
    # sampled proportional to its TD multiplier). Applying the multiplier a second time to
    # the success probability would double-count the same fact and make unreliable banks
    # look far worse than the calibration supports.

    match cause:
        case Cause.MANDATE_DEAD:
            # The honest-exception class. Nothing works, ever. An agent that keeps trying
            # here is burning money to learn something it could have inferred.
            return 0.0

        case Cause.CARD_EXPIRED:
            # A retry cannot fix an expired card, no matter how well timed. Only
            # request_card_update can, and only if the customer responds.
            return 0.0

        case Cause.RISK_BLOCK:
            return RISK_BLOCK_RETRY

        case Cause.INSUFFICIENT_FUNDS:
            # The salary cycle is the whole story. Retrying mid-month is close to
            # worthless; the same retry timed to payday is several times better.
            return _clamp(base * salary_factor(ctx.when))

        case Cause.BANK_OUTAGE:
            if ctx.health.bank_is_out(ctx.bank_id, ctx.when):
                return OUTAGE_DURING
            return _clamp(base * OUTAGE_AFTER_LIFT)

        case Cause.ROUTE_DEGRADED:
            if ctx.rail is not Rail.CARD_MANDATE or ctx.route is None:
                # Not reachable on UPI Autopay, which has no acquirer to switch.
                return _clamp(base)
            degraded = ctx.health.route_is_degraded(ctx.route, ctx.when)
            if intervention is Intervention.SWITCH_ROUTE_AND_RETRY:
                other = ctx.health.other_route(ctx.route)
                if not ctx.health.route_is_degraded(other, ctx.when):
                    return ROUTE_SWITCHED
                # Both routes degraded at once: switching does not help, and an agent
                # that has learned "switch on GW_54" will waste the attempt.
                return ROUTE_SAME
            return ROUTE_SAME if degraded else _clamp(base * 1.4)

        case Cause.AFA_TIMEOUT:
            decayed = base * AFA_LIFT * (AFA_DECAY ** (ctx.attempt_index - 1))
            return _clamp(decayed)

    raise AssertionError(f"unhandled cause {cause}")  # pragma: no cover


def debit_succeeds(
    cfg: WorldConfig, ctx: RecoveryContext, intervention: Intervention
) -> bool:
    """Resolve one debit attempt.

    The draw is keyed by ``(transaction_id, attempt_index)`` and *not* by wall-clock or
    call order, so two policies that reach the same attempt index on the same transaction
    consume the same uniform. They may face different probabilities — that is the point,
    since timing is a decision — but they do not face different luck.
    """
    p = debit_success_probability(cfg, ctx, intervention)
    return rng.bernoulli(cfg.seed, p, "debit", *_rep_key(ctx.replication), ctx.transaction_id, ctx.attempt_index)


# --- customer contact ------------------------------------------------------------------


def contact_response_probability(cfg: WorldConfig, ctx: RecoveryContext) -> float:
    """``P(customer responds to this card-update request)``.

    Segment response rates are ``chosen`` (``CALIBRATION.md`` row 39); the nearest anchor
    is row 34, "email after the first decline converts 15-25% of at-risk customers", which
    sits between the engaged and lapsed values here.

    Under the held-out regime the engaged and dormant rates are swapped, so a contact
    policy fitted on config A points at precisely the wrong customers.
    """
    segment = ctx.segment
    if cfg.regime.invert_segment_response:
        if segment is Segment.ENGAGED:
            segment = Segment.DORMANT
        elif segment is Segment.DORMANT:
            segment = Segment.ENGAGED

    base = cfg.segments[segment].contact_response_prob
    return _clamp(cfg.costs.card_update.response_prob(base, ctx.contact_index))


def card_update_succeeds(cfg: WorldConfig, ctx: RecoveryContext) -> bool:
    """A card update recovers the payment only if the cause was a fixable instrument
    *and* the customer actually responded.

    Both conditions matter economically. The first is what makes ``GW_21`` — card_expired
    against mandate_dead — the most expensive confusion in the world: on a dead mandate the
    contact is pure annoyance with no possible upside.
    """
    if ctx.true_cause is not Cause.CARD_EXPIRED:
        return False
    if ctx.rail is not Rail.CARD_MANDATE:
        return False

    responded = rng.bernoulli(
        cfg.seed,
        contact_response_probability(cfg, ctx),
        "contact-response",
        *_rep_key(ctx.replication),
        ctx.transaction_id,
        ctx.contact_index,
    )
    if not responded:
        return False
    return rng.bernoulli(
        cfg.seed,
        cfg.costs.card_update.p_success_given_response,
        "new-card-debit",
        *_rep_key(ctx.replication),
        ctx.transaction_id,
        ctx.contact_index,
    )


def contact_response_delay_hours(cfg: WorldConfig, ctx: RecoveryContext) -> float:
    """How long the customer takes to act, if they act at all.

    Censored by the mandate horizon at the call site: a response that arrives after the
    mandate is cancelled is worth nothing, which is why a late contact can be correctly
    refused even when the customer would eventually have responded.
    """
    return rng.exponential_hours(
        cfg.seed,
        cfg.costs.card_update.response_delay_median_hours,
        "contact-delay",
        *_rep_key(ctx.replication),
        ctx.transaction_id,
        ctx.contact_index,
    )


# --- human escalation ------------------------------------------------------------------


def escalation_succeeds(cfg: WorldConfig, ctx: RecoveryContext) -> bool:
    """A human can lift a risk block and can do very little else.

    That asymmetry is what makes ``GW_11`` — insufficient_funds against risk_block —
    worth diagnosing: escalation costs Rs 85 and resolves 72% of risk blocks, but spending
    it on an insufficient-funds transaction buys almost nothing.
    """
    p = (
        cfg.costs.escalation.p_resolves_risk_block
        if ctx.true_cause is Cause.RISK_BLOCK
        else cfg.costs.escalation.p_resolves_other_cause
    )
    if ctx.true_cause is Cause.MANDATE_DEAD:
        return False
    return rng.bernoulli(
        cfg.seed, p, "escalation", *_rep_key(ctx.replication), ctx.transaction_id, ctx.contact_index
    )


def escalation_delay_hours(cfg: WorldConfig, ctx: RecoveryContext) -> float:
    return rng.exponential_hours(
        cfg.seed,
        cfg.costs.escalation.queue_delay_median_hours,
        "escalation-delay",
        *_rep_key(ctx.replication),
        ctx.transaction_id,
        ctx.contact_index,
    )


# --- introspection for the world's own reports -----------------------------------------


def is_ever_recoverable(cfg: WorldConfig, ctx: RecoveryContext) -> bool:
    """Whether *any* intervention could recover this transaction, under perfect knowledge.

    Used by two things and nothing else: Baseline 3, the max-recovery ceiling, which is an
    oracle by definition; and the abandoned-but-recoverable list, which needs ground truth
    to prove the agent walked away from something it genuinely could have won.
    """
    match ctx.true_cause:
        case Cause.MANDATE_DEAD:
            return False
        case Cause.CARD_EXPIRED:
            return ctx.rail is Rail.CARD_MANDATE
        case Cause.RISK_BLOCK:
            return True
        case _:
            return True
