"""The one interface every strategy implements — baselines and the net-value agent alike.

Keeping baselines behind the same protocol is what makes the comparison honest: the
harness cannot accidentally give the agent a longer horizon, a cheaper attempt or an
extra retry, because it cannot tell them apart.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from netvalue.agent.observation import Observation


class ActionKind(StrEnum):
    RETRY_NOW = "retry_now"
    RETRY_AFTER = "retry_after"
    SWITCH_ROUTE_AND_RETRY = "switch_route_and_retry"
    REQUEST_CARD_UPDATE = "request_card_update"
    SCHEDULE_RETRY_AT = "schedule_retry_at"
    ESCALATE_TO_HUMAN = "escalate_to_human"
    ABANDON = "abandon"


class Action(BaseModel):
    """A single decision, with the reasoning that produced it.

    ``expected_value`` and ``rationale`` are not decoration: they are the audit log, and
    the audit log is the product. Every row must let a reader reconstruct why a rupee was
    spent or a transaction was abandoned.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ActionKind
    delay_hours: Annotated[float, Field(ge=0.0)] | None = None
    scheduled_at: datetime | None = None

    expected_value_inr: float | None = None
    expected_cost_inr: float | None = None
    rationale: str = ""
    gate_fired: str | None = None

    def model_post_init(self, _context: object) -> None:
        if self.kind is ActionKind.RETRY_AFTER and self.delay_hours is None:
            raise ValueError("retry_after requires delay_hours")
        if self.kind is ActionKind.SCHEDULE_RETRY_AT and self.scheduled_at is None:
            raise ValueError("schedule_retry_at requires scheduled_at")

    @property
    def is_terminal(self) -> bool:
        return self.kind is ActionKind.ABANDON


@runtime_checkable
class Policy(Protocol):
    """Implemented by every strategy in ``netvalue/policies``."""

    name: str

    def decide(self, observation: Observation) -> Action:
        """Choose the next action given only what the agent is allowed to see."""
        ...

    def reset(self) -> None:
        """Clear any per-batch state. Called once before each replication seed."""
        ...
