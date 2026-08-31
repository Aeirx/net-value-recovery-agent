"""Typed, validated, hashable world configuration.

This module is the single source of truth for every Phase 0 decision. Nothing in the
project hardcodes an economic or structural constant; it reads it from here.

Three properties are load-bearing and are enforced by validators or tests:

1. **Ambiguity.** ``P(cause | error_code)`` must never concentrate. If the error code
   identified the cause there would be no inference problem and no reason for a model
   in the system. See :meth:`WorldConfig.posterior_cause_given_code` and
   ``tests/test_ambiguity.py``.
2. **Economics.** The customer-annoyance cost is *derived* from a churn hazard and a
   customer lifetime value, never chosen as a rupee figure. See
   :meth:`WorldConfig.annoyance_cost` and ``tests/test_economics.py``.
3. **Reproducibility.** Every run records :meth:`WorldConfig.config_hash`, so a reported
   number can always be traced to the exact parameters that produced it.

Flags used in comments below match ``PHASE0_DECISIONS.md``:
``[DESIGN]`` a modelling choice, ``[VERIFY-P2]`` asserted as fact and pending sourcing,
``[DERIVED]`` computed from other parameters.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Tolerance for "these probabilities sum to one" checks.
_SUM_TOL = 1e-9

Probability = Annotated[float, Field(ge=0.0, le=1.0)]
NonNegative = Annotated[float, Field(ge=0.0)]


# --------------------------------------------------------------------------------------
# Enumerations — DECISION-001, 003, 005
# --------------------------------------------------------------------------------------


class Rail(StrEnum):
    """Payment rail. Both are recurring; one-shot checkout is deliberately out of scope."""

    CARD_MANDATE = "card_mandate"
    UPI_AUTOPAY = "upi_autopay"


class DeclineClass(StrEnum):
    """NPCI's own taxonomy. BD is user-side, TD is bank or network infrastructure."""

    BD = "business_decline"
    TD = "technical_decline"


class Cause(StrEnum):
    """The seven hidden ground-truth causes. Never observable by the agent."""

    INSUFFICIENT_FUNDS = "insufficient_funds"
    CARD_EXPIRED = "card_expired"
    RISK_BLOCK = "risk_block"
    MANDATE_DEAD = "mandate_dead"

    #: The authorisation step did not complete. Rescoped in Phase 2: above the
    #: Rs 15,000 AFA threshold this is an authentication timeout, and below it — where no
    #: AFA is required — it is the customer exercising the opt-out that the 2026 framework
    #: attaches to every pre-debit notification. Phase 0 scoped this to AFA alone, which
    #: would have made it impossible on a plan ladder topping out at Rs 4,999.
    AFA_TIMEOUT = "afa_timeout"

    BANK_OUTAGE = "bank_outage"
    ROUTE_DEGRADED = "route_degraded"


class ErrorCode(StrEnum):
    """What the agent actually sees. Deliberately one-to-many against Cause."""

    GW_05 = "GW_05"  # declined by issuer
    GW_11 = "GW_11"  # do not honour
    GW_21 = "GW_21"  # instrument invalid
    GW_33 = "GW_33"  # authentication not completed
    GW_54 = "GW_54"  # upstream timeout
    GW_91 = "GW_91"  # issuer unavailable


class Intervention(StrEnum):
    RETRY_NOW = "retry_now"
    RETRY_AFTER = "retry_after"
    SWITCH_ROUTE_AND_RETRY = "switch_route_and_retry"
    REQUEST_CARD_UPDATE = "request_card_update"
    SCHEDULE_RETRY_AT = "schedule_retry_at"
    ESCALATE_TO_HUMAN = "escalate_to_human"
    ABANDON = "abandon"


class Segment(StrEnum):
    """Customer engagement segment. Drives contact-response behaviour and remaining LTV."""

    ENGAGED = "engaged"
    LAPSED = "lapsed"
    DORMANT = "dormant"


# --------------------------------------------------------------------------------------
# Component models
# --------------------------------------------------------------------------------------


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CauseSpec(_Frozen):
    """DECISION-003, DECISION-004."""

    decline_class: DeclineClass
    rails: tuple[Rail, ...]
    prior: Probability
    recoverable_by_retry: bool
    permanently_unrecoverable: bool = False

    @model_validator(mode="after")
    def _check_coherent(self) -> Self:
        if self.permanently_unrecoverable and self.recoverable_by_retry:
            raise ValueError("a permanently unrecoverable cause cannot be retry-recoverable")
        if not self.rails:
            raise ValueError("a cause must apply to at least one rail")
        return self


class PlanTier(_Frozen):
    """DECISION-007. The left tail is what makes value-destroying recoveries abundant."""

    name: str
    amount_inr: NonNegative
    share: Probability


class SegmentSpec(_Frozen):
    """DECISION-008/009 inputs. Remaining cycles set LTV; response probability sets
    whether a customer contact can plausibly pay for itself."""

    share: Probability
    expected_remaining_cycles: NonNegative
    contact_response_prob: Probability


class AnnoyanceModel(_Frozen):
    """DECISION-009. The single most attackable number in the project, so it is not a
    number: annoyance is ``delta_churn(k) * ltv_remaining``, convex in contact index.

    Convexity is the realistic shape (dunning fatigue is not linear) and it is what makes
    stopping a genuine decision rather than a formality.
    """

    delta_churn_by_contact: tuple[Probability, ...]  # index 0 == first contact
    delta_churn_beyond: Probability

    @model_validator(mode="after")
    def _check_convex(self) -> Self:
        seq = (*self.delta_churn_by_contact, self.delta_churn_beyond)
        if any(b <= a for a, b in itertools.pairwise(seq)):
            raise ValueError("delta_churn must be strictly increasing in contact index")
        return self

    def delta_churn(self, contact_index: int) -> float:
        """``contact_index`` is 1-based: 1 is the first contact sent."""
        if contact_index < 1:
            raise ValueError("contact_index is 1-based")
        if contact_index <= len(self.delta_churn_by_contact):
            return self.delta_churn_by_contact[contact_index - 1]
        return self.delta_churn_beyond


class InterventionSpec(_Frozen):
    """DECISION-006 five-tuple: cost, annoyance, latency, success model, preconditions.

    The success model itself lives in ``world/recovery.py`` (hidden physics, Phase 3) and
    in ``agent/estimator.py`` (the agent's fitted estimate, Phase 5). What is recorded
    here is everything *outside* the success model.
    """

    flat_cost_inr: NonNegative
    consumes_contact: bool
    latency_hours_median: NonNegative | None
    rails: tuple[Rail, ...]
    max_uses_per_transaction: int | None = None


class CardUpdateModel(_Frozen):
    """Response process for ``request_card_update``. A contact is only worth sending if
    the customer might actually respond before the mandate expires."""

    response_delay_median_hours: NonNegative
    p_success_given_response: Probability
    # Multiplier on the segment's base response probability, by contact index.
    repeat_response_decay: tuple[Probability, ...]

    @model_validator(mode="after")
    def _check_monotone(self) -> Self:
        d = self.repeat_response_decay
        if not d or d[0] != 1.0:
            raise ValueError("the first contact must carry an undecayed response probability")
        if any(b >= a for a, b in itertools.pairwise(d)):
            raise ValueError("repeat response decay must be strictly decreasing")
        return self

    def response_prob(self, base: float, contact_index: int) -> float:
        if contact_index < 1:
            raise ValueError("contact_index is 1-based")
        idx = min(contact_index, len(self.repeat_response_decay)) - 1
        return base * self.repeat_response_decay[idx]


class EscalationModel(_Frozen):
    """Response process for ``escalate_to_human``. Note there is deliberately no minimum
    amount: whether a human should touch a small recovery is exactly the judgment the
    value engine exists to make."""

    queue_delay_median_hours: NonNegative
    p_resolves_risk_block: Probability
    p_resolves_other_cause: Probability


class CostModel(_Frozen):
    """DECISION-008 through DECISION-010."""

    mdr_by_rail: dict[Rail, Probability]
    p_involuntary_churn: Probability
    annoyance: AnnoyanceModel
    interventions: dict[Intervention, InterventionSpec]
    card_update: CardUpdateModel
    escalation: EscalationModel


class Bounds(_Frozen):
    """DECISION-012. Deterministic, and able only to *shrink* the admissible action set.

    A model may not be the thing that enforces a cap: bounds you cannot verify are not
    bounds. Every gate logs when it fires.

    Phase 2 separated these into two kinds, because conflating them was a real error:

    * **Regulatory or network bounds** are external facts, sourced in ``CALIBRATION.md``.
      ``min_inter_attempt_hours`` and ``network_retry_cap_per_30d`` are these.
    * **Merchant-policy bounds** are ours to choose. ``max_debits_per_mandate_cycle`` is
      one, and Phase 0 wrongly asserted it as a network rule. India's e-mandate framework
      caps no such thing.
    """

    max_attempts_per_transaction: int = Field(gt=0)
    max_contacts_per_transaction: int = Field(gt=0)

    #: Merchant policy, not a rule. [chosen] See CALIBRATION.md row 4.
    max_debits_per_mandate_cycle: int = Field(gt=0)

    #: Binding network cap across a 30-day window. Mastercard is the stricter of the two
    #: (10 vs Visa's 15), so it is the one that binds. [sourced]
    network_retry_cap_per_30d: int = Field(gt=0)

    #: RBI Digital Payments E-mandate Framework 2026: every retry must be preceded by a
    #: fresh pre-debit notification at least 24h ahead, and no retry may occur inside 24h
    #: of the failure. This is a hard regulatory floor, not a tuning knob. [sourced]
    min_inter_attempt_hours: NonNegative
    pre_debit_notification_hours: NonNegative

    #: Per-transaction value above which additional-factor authentication is required on
    #: both rails. Below it, recurring debits proceed unauthenticated. [sourced]
    afa_threshold_inr: NonNegative

    retry_storm_max_per_bank_hour: int = Field(gt=0)
    card_expired_retry_limit: int = Field(ge=0)
    card_expired_belief_threshold: Probability
    expiry_horizon_days_by_rail: dict[Rail, int]

    @model_validator(mode="after")
    def _check_regulatory_floor(self) -> Self:
        if self.min_inter_attempt_hours < self.pre_debit_notification_hours:
            raise ValueError(
                "min_inter_attempt_hours cannot be shorter than the mandatory pre-debit "
                "notification window: a retry inside it would be non-compliant"
            )
        if self.max_debits_per_mandate_cycle > self.network_retry_cap_per_30d:
            raise ValueError(
                "merchant policy allows more debits per cycle than the network permits "
                "in 30 days"
            )
        return self


class ClockConfig(_Frozen):
    """DECISION-002. A single global discrete-event clock. Without shared time a bank
    outage is a per-transaction attribute correlated with nothing, and the correlated
    multi-bank outage in config B could not exist."""

    start: datetime
    end: datetime
    tick_hours: int = Field(gt=0)
    timezone: str

    @model_validator(mode="after")
    def _check_window(self) -> Self:
        if self.end <= self.start:
            raise ValueError("clock window must be non-empty")
        return self


class AmbiguityThresholds(_Frozen):
    """DECISION-006. Asserted in ``tests/test_ambiguity.py``.

    Margins are deliberately thin. If the generator drifts, the test fires — which is the
    entire point of encoding the constraint rather than intending it.
    """

    max_posterior_mass_per_code: Probability
    min_conditional_entropy_bits: NonNegative
    max_mutual_information_bits: NonNegative


# --------------------------------------------------------------------------------------
# Root config
# --------------------------------------------------------------------------------------


class WorldConfig(_Frozen):
    """The whole world, in one hashable object."""

    name: str
    seed: int
    n_transactions: int = Field(gt=0)

    rail_shares: dict[Rail, Probability]

    #: Share of scheduled debits that fail on each rail, before any recovery. Sourced in
    #: Phase 2 and consumed by the Phase 3 generator. The gap is large and real: UPI
    #: Autopay is stateless per debit while card mandates are bank-managed, so the two
    #: rails must not share a failure profile. [sourced]
    base_failure_rate_by_rail: dict[Rail, Probability]

    causes: dict[Cause, CauseSpec]
    code_given_cause: dict[Cause, dict[ErrorCode, Probability]]
    plans: tuple[PlanTier, ...]
    segments: dict[Segment, SegmentSpec]

    costs: CostModel
    bounds: Bounds
    clock: ClockConfig
    ambiguity: AmbiguityThresholds

    bd_share_target: Probability
    bd_share_tolerance: Probability

    # ---------------------------------------------------------------- validation

    @model_validator(mode="after")
    def _check_distributions(self) -> Self:
        def _sums_to_one(label: str, values: list[float]) -> None:
            total = math.fsum(values)
            if abs(total - 1.0) > 1e-6:
                raise ValueError(f"{label} must sum to 1.0, got {total!r}")

        _sums_to_one("rail_shares", list(self.rail_shares.values()))
        _sums_to_one("cause priors", [c.prior for c in self.causes.values()])
        _sums_to_one("plan shares", [p.share for p in self.plans])
        _sums_to_one("segment shares", [s.share for s in self.segments.values()])

        if set(self.causes) != set(Cause):
            raise ValueError("every Cause needs a CauseSpec")
        if set(self.code_given_cause) != set(Cause):
            raise ValueError("every Cause needs a P(code | cause) row")

        for cause, row in self.code_given_cause.items():
            if set(row) != set(ErrorCode):
                raise ValueError(f"P(code | {cause}) must cover every ErrorCode")
            _sums_to_one(f"P(code | {cause})", list(row.values()))

        return self

    @model_validator(mode="after")
    def _check_bd_td_split(self) -> Self:
        bd = math.fsum(
            spec.prior
            for spec in self.causes.values()
            if spec.decline_class is DeclineClass.BD
        )
        if abs(bd - self.bd_share_target) > self.bd_share_tolerance:
            raise ValueError(
                f"implied BD share {bd:.4f} is outside "
                f"{self.bd_share_target} +/- {self.bd_share_tolerance}"
            )
        return self

    @model_validator(mode="after")
    def _check_rail_coherence(self) -> Self:
        for cause, spec in self.causes.items():
            unknown = set(spec.rails) - set(self.rail_shares)
            if unknown:
                raise ValueError(f"cause {cause} references unconfigured rails {unknown}")
        for rail in self.rail_shares:
            if rail not in self.bounds.expiry_horizon_days_by_rail:
                raise ValueError(f"rail {rail} has no expiry horizon")
            if rail not in self.costs.mdr_by_rail:
                raise ValueError(f"rail {rail} has no MDR")
            if rail not in self.base_failure_rate_by_rail:
                raise ValueError(f"rail {rail} has no base failure rate")
        return self

    # ---------------------------------------------------------------- identity

    def canonical_json(self) -> str:
        """Deterministic serialisation. Key order fixed, floats not reformatted."""
        payload: dict[str, Any] = self.model_dump(mode="json")
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def config_hash(self) -> str:
        """Recorded in every run manifest, so any number traces to its parameters."""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def write_json(self, path: str | Path) -> str:
        """Write the published config.

        The newline is pinned to ``\\n`` deliberately: with the platform default this file
        picks up CRLF on Windows and LF in CI, and the "config is committed and current"
        CI check would then fail on line endings rather than on a real parameter drift.
        """
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(self.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
        with p.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(body)
        return self.config_hash()

    @classmethod
    def read_json(cls, path: str | Path) -> WorldConfig:
        return cls.model_validate_json(Path(path).read_text())

    # ---------------------------------------------------------------- ambiguity analytics

    def code_marginal(self) -> dict[ErrorCode, float]:
        """``P(code)`` under the cause priors. [DERIVED]"""
        out = {code: 0.0 for code in ErrorCode}
        for cause, spec in self.causes.items():
            for code, p in self.code_given_cause[cause].items():
                out[code] += spec.prior * p
        return out

    def posterior_cause_given_code(self) -> dict[ErrorCode, dict[Cause, float]]:
        """``P(cause | code)`` — what the agent can infer from the code alone. [DERIVED]

        This is the table the whole project's premise rests on. If any row concentrates,
        there is no inference problem left to solve.
        """
        marginal = self.code_marginal()
        out: dict[ErrorCode, dict[Cause, float]] = {}
        for code in ErrorCode:
            denom = marginal[code]
            if denom <= 0.0:
                raise ValueError(f"error code {code} is unreachable under these priors")
            out[code] = {
                cause: (spec.prior * self.code_given_cause[cause][code]) / denom
                for cause, spec in self.causes.items()
            }
        return out

    @staticmethod
    def _entropy_bits(dist: dict[Cause, float]) -> float:
        return -math.fsum(p * math.log2(p) for p in dist.values() if p > 0.0)

    def cause_entropy_bits(self) -> float:
        """``H(cause)`` — uncertainty before seeing the code. [DERIVED]"""
        return self._entropy_bits({c: s.prior for c, s in self.causes.items()})

    def conditional_entropy_by_code_bits(self) -> dict[ErrorCode, float]:
        """``H(cause | code = c)`` for each code. [DERIVED]"""
        post = self.posterior_cause_given_code()
        return {code: self._entropy_bits(post[code]) for code in ErrorCode}

    def conditional_entropy_bits(self) -> float:
        """``H(cause | code)`` averaged over codes. [DERIVED]"""
        marginal = self.code_marginal()
        per_code = self.conditional_entropy_by_code_bits()
        return math.fsum(marginal[code] * per_code[code] for code in ErrorCode)

    def mutual_information_bits(self) -> float:
        """``I(cause; code)``. High means the code gives the answer away. [DERIVED]"""
        return self.cause_entropy_bits() - self.conditional_entropy_bits()

    # ---------------------------------------------------------------- economics analytics

    def ltv_remaining(self, amount_inr: float, segment: Segment) -> float:
        """Expected remaining subscription value. [DERIVED]"""
        return amount_inr * self.segments[segment].expected_remaining_cycles

    def recovery_value(self, amount_inr: float, rail: Rail, ltv_remaining: float) -> float:
        """DECISION-008. A failed renewal that is never recovered is involuntary churn,
        not a missed invoice. Modelling recovery value as the invoice alone would produce
        a degenerate agent that abandons everything — the mirror of the failure mode this
        project exists to avoid.
        """
        net_invoice = amount_inr * (1.0 - self.costs.mdr_by_rail[rail])
        return net_invoice + self.costs.p_involuntary_churn * ltv_remaining

    def annoyance_cost(self, contact_index: int, ltv_remaining: float) -> float:
        """DECISION-009. Derived, never chosen. [DERIVED]"""
        return self.costs.annoyance.delta_churn(contact_index) * ltv_remaining

    def required_recovery_prob(
        self,
        contact_index: int,
        amount_inr: float,
        ltv_remaining: float,
        rail: Rail,
        flat_cost_inr: float,
    ) -> float:
        """Exact break-even probability for sending contact ``k``. [DERIVED]"""
        value = self.recovery_value(amount_inr, rail, ltv_remaining)
        if value <= 0.0:
            return math.inf
        cost = flat_cost_inr + self.annoyance_cost(contact_index, ltv_remaining)
        return cost / value

    def required_recovery_prob_asymptotic(self, contact_index: int) -> float:
        """The LTV-dominated limit, where the invoice and flat cost fall away.

        ``ltv_remaining`` cancels almost entirely, leaving an amount-independent policy
        boundary — the four numbers that are the thesis:

            contact 1 ~ 1.5%   contact 2 ~ 4.5%   contact 3 ~ 12.7%   contact 4 ~ 25.5%

        Nothing hardcodes these. They fall out of the economics, which is the point.
        """
        return self.costs.annoyance.delta_churn(contact_index) / self.costs.p_involuntary_churn


# --------------------------------------------------------------------------------------
# Config A — the tuning regime. Every value traces to PHASE0_DECISIONS.md.
# --------------------------------------------------------------------------------------


def default_config_a() -> WorldConfig:
    """The tuning regime. Config B (held-out, Phase 3) derives from this."""
    return WorldConfig(
        name="config_a",
        seed=20260831,
        n_transactions=400,
        # DECISION-001 [DESIGN]
        rail_shares={Rail.CARD_MANDATE: 0.65, Rail.UPI_AUTOPAY: 0.35},
        # Midpoints of the published ranges: UPI Autopay 8-15%, card mandates 2-3%.
        # [sourced] See CALIBRATION.md row 11.
        base_failure_rate_by_rail={Rail.CARD_MANDATE: 0.025, Rail.UPI_AUTOPAY: 0.115},
        # DECISION-003, DECISION-004 [DESIGN], BD/TD target [VERIFY-P2]
        causes={
            Cause.INSUFFICIENT_FUNDS: CauseSpec(
                decline_class=DeclineClass.BD,
                rails=(Rail.CARD_MANDATE, Rail.UPI_AUTOPAY),
                prior=0.34,
                recoverable_by_retry=True,
            ),
            Cause.AFA_TIMEOUT: CauseSpec(
                decline_class=DeclineClass.BD,
                rails=(Rail.CARD_MANDATE, Rail.UPI_AUTOPAY),
                prior=0.15,
                recoverable_by_retry=True,
            ),
            Cause.CARD_EXPIRED: CauseSpec(
                decline_class=DeclineClass.BD,
                rails=(Rail.CARD_MANDATE,),
                prior=0.12,
                recoverable_by_retry=False,  # only request_card_update can help
            ),
            Cause.RISK_BLOCK: CauseSpec(
                decline_class=DeclineClass.BD,
                rails=(Rail.CARD_MANDATE, Rail.UPI_AUTOPAY),
                prior=0.10,
                recoverable_by_retry=False,  # only escalate_to_human can help
            ),
            Cause.MANDATE_DEAD: CauseSpec(
                decline_class=DeclineClass.BD,
                rails=(Rail.CARD_MANDATE, Rail.UPI_AUTOPAY),
                prior=0.07,
                recoverable_by_retry=False,
                permanently_unrecoverable=True,  # the honest-exception class
            ),
            Cause.BANK_OUTAGE: CauseSpec(
                decline_class=DeclineClass.TD,
                rails=(Rail.CARD_MANDATE, Rail.UPI_AUTOPAY),
                prior=0.11,
                recoverable_by_retry=True,
            ),
            Cause.ROUTE_DEGRADED: CauseSpec(
                decline_class=DeclineClass.TD,
                rails=(Rail.CARD_MANDATE,),
                prior=0.11,
                recoverable_by_retry=True,
            ),
        },
        # DECISION-005 [DESIGN] — rows are P(code | cause) and each sums to 1.
        # The three economically loaded ambiguities:
        #   GW_11  insufficient_funds vs risk_block   (timed retry vs escalate)
        #   GW_21  card_expired vs mandate_dead       (paid contact vs abandon)
        #   GW_54  route_degraded vs bank_outage      (switch route vs wait it out)
        code_given_cause={
            Cause.INSUFFICIENT_FUNDS: {
                ErrorCode.GW_05: 0.58, ErrorCode.GW_11: 0.19, ErrorCode.GW_21: 0.02,
                ErrorCode.GW_33: 0.06, ErrorCode.GW_54: 0.05, ErrorCode.GW_91: 0.10,
            },
            Cause.CARD_EXPIRED: {
                ErrorCode.GW_05: 0.30, ErrorCode.GW_11: 0.05, ErrorCode.GW_21: 0.55,
                ErrorCode.GW_33: 0.00, ErrorCode.GW_54: 0.05, ErrorCode.GW_91: 0.05,
            },
            Cause.RISK_BLOCK: {
                ErrorCode.GW_05: 0.32, ErrorCode.GW_11: 0.44, ErrorCode.GW_21: 0.04,
                ErrorCode.GW_33: 0.12, ErrorCode.GW_54: 0.04, ErrorCode.GW_91: 0.04,
            },
            Cause.MANDATE_DEAD: {
                ErrorCode.GW_05: 0.20, ErrorCode.GW_11: 0.08, ErrorCode.GW_21: 0.60,
                ErrorCode.GW_33: 0.00, ErrorCode.GW_54: 0.05, ErrorCode.GW_91: 0.07,
            },
            Cause.AFA_TIMEOUT: {
                ErrorCode.GW_05: 0.10, ErrorCode.GW_11: 0.03, ErrorCode.GW_21: 0.02,
                ErrorCode.GW_33: 0.70, ErrorCode.GW_54: 0.13, ErrorCode.GW_91: 0.02,
            },
            Cause.BANK_OUTAGE: {
                ErrorCode.GW_05: 0.12, ErrorCode.GW_11: 0.03, ErrorCode.GW_21: 0.01,
                ErrorCode.GW_33: 0.06, ErrorCode.GW_54: 0.30, ErrorCode.GW_91: 0.48,
            },
            Cause.ROUTE_DEGRADED: {
                ErrorCode.GW_05: 0.15, ErrorCode.GW_11: 0.05, ErrorCode.GW_21: 0.02,
                ErrorCode.GW_33: 0.08, ErrorCode.GW_54: 0.50, ErrorCode.GW_91: 0.20,
            },
        },
        # DECISION-007 [DESIGN] — 65% of volume at or below Rs 149.
        plans=(
            PlanTier(name="basic", amount_inr=99.0, share=0.42),
            PlanTier(name="standard", amount_inr=149.0, share=0.23),
            PlanTier(name="plus", amount_inr=299.0, share=0.18),
            PlanTier(name="premium", amount_inr=599.0, share=0.11),
            PlanTier(name="annual_lite", amount_inr=1499.0, share=0.04),
            PlanTier(name="annual_pro", amount_inr=4999.0, share=0.02),
        ),
        segments={
            Segment.ENGAGED: SegmentSpec(
                share=0.45, expected_remaining_cycles=14.0, contact_response_prob=0.42
            ),
            Segment.LAPSED: SegmentSpec(
                share=0.35, expected_remaining_cycles=7.0, contact_response_prob=0.18
            ),
            Segment.DORMANT: SegmentSpec(
                share=0.20, expected_remaining_cycles=4.0, contact_response_prob=0.06
            ),
        },
        costs=CostModel(
            # DECISION-008 [VERIFY-P2] — UPI Autopay carries no MDR, so a recovered UPI
            # renewal is worth strictly more net than an identical card renewal. The agent
            # should behave differently by rail, and that is free evidence it is working.
            mdr_by_rail={Rail.CARD_MANDATE: 0.020, Rail.UPI_AUTOPAY: 0.000},
            p_involuntary_churn=0.55,  # [VERIFY-P2] — scales every recovery value
            # DECISION-009 [VERIFY-P2] — the parameter the whole submission rests on, and
            # precisely the one the Phase 8 sensitivity sweep exists to neutralise.
            annoyance=AnnoyanceModel(
                delta_churn_by_contact=(0.008, 0.025, 0.070),
                delta_churn_beyond=0.140,
            ),
            # DECISION-006 five-tuples [DESIGN]
            interventions={
                Intervention.RETRY_NOW: InterventionSpec(
                    flat_cost_inr=0.60, consumes_contact=False, latency_hours_median=0.0,
                    rails=(Rail.CARD_MANDATE, Rail.UPI_AUTOPAY),
                ),
                Intervention.RETRY_AFTER: InterventionSpec(
                    flat_cost_inr=0.60, consumes_contact=False, latency_hours_median=None,
                    rails=(Rail.CARD_MANDATE, Rail.UPI_AUTOPAY),
                ),
                Intervention.SWITCH_ROUTE_AND_RETRY: InterventionSpec(
                    flat_cost_inr=0.90, consumes_contact=False, latency_hours_median=0.0,
                    rails=(Rail.CARD_MANDATE,),  # no acquirer switch exists on UPI Autopay
                    max_uses_per_transaction=2,
                ),
                Intervention.REQUEST_CARD_UPDATE: InterventionSpec(
                    flat_cost_inr=0.60, consumes_contact=True, latency_hours_median=26.0,
                    rails=(Rail.CARD_MANDATE,),
                    max_uses_per_transaction=3,
                ),
                Intervention.SCHEDULE_RETRY_AT: InterventionSpec(
                    flat_cost_inr=0.60, consumes_contact=False, latency_hours_median=None,
                    rails=(Rail.CARD_MANDATE, Rail.UPI_AUTOPAY),
                ),
                Intervention.ESCALATE_TO_HUMAN: InterventionSpec(
                    # ~7 min at Rs 730/hr fully loaded [VERIFY-P2]. Deliberately no minimum
                    # amount precondition: whether a human should touch a Rs 99 recovery is
                    # exactly the judgment the value engine exists to make.
                    flat_cost_inr=85.00, consumes_contact=True, latency_hours_median=9.0,
                    rails=(Rail.CARD_MANDATE, Rail.UPI_AUTOPAY),
                    max_uses_per_transaction=1,
                ),
                Intervention.ABANDON: InterventionSpec(
                    flat_cost_inr=0.0, consumes_contact=False, latency_hours_median=None,
                    rails=(Rail.CARD_MANDATE, Rail.UPI_AUTOPAY),
                ),
            },
            card_update=CardUpdateModel(
                response_delay_median_hours=26.0,
                p_success_given_response=0.94,
                repeat_response_decay=(1.00, 0.58, 0.42),
            ),
            escalation=EscalationModel(
                queue_delay_median_hours=9.0,
                p_resolves_risk_block=0.72,
                p_resolves_other_cause=0.05,
            ),
        ),
        # DECISION-012, revised in Phase 2 against the sourced record.
        bounds=Bounds(
            max_attempts_per_transaction=6,  # a ceiling, not a policy [DESIGN]
            max_contacts_per_transaction=3,  # [DESIGN]
            # Phase 0 asserted this as a network rule. It is not: India's e-mandate
            # framework caps no number of retries per cycle. Kept as merchant policy and
            # relabelled [chosen]. See CALIBRATION.md row 4.
            max_debits_per_mandate_cycle=3,
            # Mastercard 10 / 30d is stricter than Visa's 15, so it binds. [sourced]
            network_retry_cap_per_30d=10,
            # Was 4.0. RBI's 2026 e-mandate framework requires a fresh pre-debit
            # notification at least 24h before every attempt, so a 4h retry is not
            # merely aggressive, it is non-compliant. [sourced]
            min_inter_attempt_hours=24.0,
            pre_debit_notification_hours=24.0,
            afa_threshold_inr=15000.0,  # [sourced]
            retry_storm_max_per_bank_hour=2,  # [DESIGN]
            card_expired_retry_limit=1,  # [DESIGN]
            card_expired_belief_threshold=0.50,  # [DESIGN]
            expiry_horizon_days_by_rail={  # [chosen] — no published force-cancel rule found
                Rail.CARD_MANDATE: 21,
                Rail.UPI_AUTOPAY: 14,
            },
        ),
        # DECISION-002 [DESIGN] — window contains both salary dates and the 31 Mar
        # financial-year-end bank closing spike [VERIFY-P2].
        clock=ClockConfig(
            start=datetime(2026, 3, 5, 0, 0),
            end=datetime(2026, 4, 5, 0, 0),
            tick_hours=1,
            timezone="Asia/Kolkata",
        ),
        # DECISION-006 — margins are thin on purpose. If the world drifts, the test fires.
        ambiguity=AmbiguityThresholds(
            max_posterior_mass_per_code=0.70,
            min_conditional_entropy_bits=1.40,
            max_mutual_information_bits=1.00,
        ),
        bd_share_target=0.78,
        bd_share_tolerance=0.02,
    )


CONFIG_A: WorldConfig = default_config_a()
