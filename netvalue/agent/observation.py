"""DECISION-013 — the boundary contract.

This module defines *everything* the agent is allowed to see. It is the observable half
of the world/agent split that ``tests/test_boundary.py`` enforces.

What is deliberately absent is as important as what is present: no ``true_cause``, no
``recovery_probability``, no ``bank_health``, no ``outage_window``, no
``will_respond_to_contact``. Those live in ``netvalue.world`` and the agent never reads
them. Recovery probabilities reach the agent only through ``agent/estimator.py``, fitted
on observed historical outcomes.

Note that this module imports nothing from ``netvalue.world`` — not even the enums. The
observable vocabulary is redeclared here on purpose, so the contract stands on its own
and the boundary test can never be satisfied vacuously.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class ObservedRail(StrEnum):
    CARD_MANDATE = "card_mandate"
    UPI_AUTOPAY = "upi_autopay"


class ObservedErrorCode(StrEnum):
    GW_05 = "GW_05"
    GW_11 = "GW_11"
    GW_21 = "GW_21"
    GW_33 = "GW_33"
    GW_54 = "GW_54"
    GW_91 = "GW_91"


class ObservedSegment(StrEnum):
    ENGAGED = "engaged"
    LAPSED = "lapsed"
    DORMANT = "dormant"


class AttemptOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PENDING = "pending"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PriorAttempt(_Frozen):
    """One prior action and what came of it. The agent's only evidence about dynamics."""

    at: datetime
    intervention: str
    error_code: ObservedErrorCode | None
    outcome: AttemptOutcome


class CustomerHistory(_Frozen):
    """The signal that separates causes an error code cannot.

    ``GW_05`` covers insufficient funds, risk block and a degraded route. Only this
    block distinguishes them, which is why the diagnosis step needs a model at all.
    """

    successful_debits_12m: Annotated[int, Field(ge=0)]
    failed_debits_12m: Annotated[int, Field(ge=0)]
    last_success_at: datetime | None
    avg_days_late: Annotated[float, Field(ge=0.0)]
    prior_contact_responses: Annotated[int, Field(ge=0)]
    prior_contacts_sent: Annotated[int, Field(ge=0)]
    segment_label: ObservedSegment


class Observation(_Frozen):
    """The complete view handed to the agent at each decision point."""

    transaction_id: str
    merchant_id: str
    customer_id: str

    rail: ObservedRail
    amount_inr: Annotated[float, Field(gt=0.0)]
    plan_tenure_months: Annotated[int, Field(ge=0)]

    error_code: ObservedErrorCode
    error_message: str  # free text, noisy, non-canonical — deliberately unreliable
    bank_id: str

    card_last4: str | None = None
    card_network: str | None = None
    card_exp_month: Annotated[int, Field(ge=1, le=12)] | None = None
    card_exp_year: Annotated[int, Field(ge=2000, le=2100)] | None = None

    mandate_id: str
    mandate_created_at: datetime
    mandate_debits_this_cycle: Annotated[int, Field(ge=0)]

    attempt_number: Annotated[int, Field(ge=1)]
    prior_attempts: tuple[PriorAttempt, ...] = ()
    customer_history: CustomerHistory

    first_failure_at: datetime
    expires_at: datetime
    observed_at: datetime

    @property
    def contacts_used(self) -> int:
        """Contacts already spent on this transaction."""
        return self.customer_history.prior_contacts_sent

    @property
    def hours_remaining(self) -> float:
        """Time left before the mandate is force-cancelled. The DP horizon."""
        return max(0.0, (self.expires_at - self.observed_at).total_seconds() / 3600.0)

    @property
    def is_card(self) -> bool:
        return self.rail is ObservedRail.CARD_MANDATE
