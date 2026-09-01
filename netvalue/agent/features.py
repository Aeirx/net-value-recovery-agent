"""Feature extraction for the recovery estimator.

Everything here is computable from an :class:`Observation` at decision time, or from a
logged row in ``data/history.jsonl``. Nothing is computable only from ground truth — that
is the whole point, and ``tests/test_boundary.py`` enforces that this module imports
nothing from ``netvalue.world``.

**One judgement call worth naming.** ``days_to_salary`` is distance to the nearest 1st or
7th of the month. Those dates are not a secret of the simulator: Indian payroll clusters
there, and any real dunning team would compute the same feature from a calendar. What the
agent must *not* have is the size of the effect — how much more likely a debit is to clear
on payday — and that is learned from logged outcomes, not assumed. The calendar arithmetic
is duplicated here rather than imported from ``world/calendar.py`` so the boundary stays
absolute even for public knowledge.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from netvalue.agent.observation import Observation
from netvalue.agent.policy import ActionKind

#: Days of the month on which Indian payroll clusters. Public knowledge, not world state.
SALARY_DAYS: tuple[int, ...] = (1, 7)


def days_to_nearest_salary_day(when: datetime) -> int:
    """Distance in days to the nearest payroll date, wrapping across month boundaries."""
    day = when.day
    best = 99
    for offset in (-31, 0, 31):
        for salary_day in SALARY_DAYS:
            best = min(best, abs(day - (salary_day + offset)))
    return best


def _bucket(value: float, edges: tuple[float, ...], labels: tuple[str, ...]) -> str:
    """Left-closed bucketing. ``labels`` must be one longer than ``edges``."""
    if len(labels) != len(edges) + 1:
        raise ValueError("labels must be one longer than edges")
    for i, edge in enumerate(edges):
        if value < edge:
            return labels[i]
    return labels[-1]


def attempt_bucket(attempt_index: int) -> str:
    return _bucket(float(attempt_index), (2.0, 3.0), ("1", "2", "3+"))


def contact_bucket(contact_index: int) -> str:
    return _bucket(float(contact_index), (1.0, 2.0, 3.0), ("0", "1", "2", "3+"))


def salary_bucket(days_to_salary: int) -> str:
    """Payday, its shoulder, or the mid-cycle trough."""
    return _bucket(float(days_to_salary), (2.0, 5.0), ("payday", "near", "trough"))


def late_bucket(avg_days_late: float) -> str:
    """Whether this customer historically pays on time. The strongest single cue for
    insufficient funds, and unavailable from the error code."""
    return _bucket(avg_days_late, (3.0,), ("prompt", "late"))


def responsiveness_bucket(responses: int, sent: int) -> str:
    """Whether contacting this customer has ever worked before.

    Directly relevant to whether a contact can pay for itself, and the estimator has to
    learn its weight rather than be told it.
    """
    if sent == 0:
        return "unknown"
    return "responsive" if responses > 0 else "silent"


@dataclass(frozen=True, slots=True)
class Features:
    """The estimator's view of a decision. Deliberately coarse: cells must stay populated."""

    intervention: str
    rail: str
    error_code: str
    attempt: str
    contact: str
    segment: str
    salary: str
    late: str
    responsiveness: str
    expiry_past: bool

    def as_tuple(self, *names: str) -> tuple[object, ...]:
        return tuple(getattr(self, name) for name in names)


def card_expiry_visibly_past(observation: Observation) -> bool:
    if observation.card_exp_year is None or observation.card_exp_month is None:
        return False
    ref = observation.observed_at
    return (observation.card_exp_year, observation.card_exp_month) < (ref.year, ref.month)


def from_observation(
    observation: Observation, action: ActionKind, *, contact_index: int | None = None
) -> Features:
    """Features for "if I take this action now, does it work?"."""
    contacts = observation.contacts_used if contact_index is None else contact_index - 1
    history = observation.customer_history
    return Features(
        intervention=action.value,
        rail=observation.rail.value,
        error_code=observation.error_code.value,
        attempt=attempt_bucket(observation.attempt_number),
        contact=contact_bucket(contacts + 1),
        segment=history.segment_label.value,
        salary=salary_bucket(days_to_nearest_salary_day(observation.observed_at)),
        late=late_bucket(history.avg_days_late),
        responsiveness=responsiveness_bucket(
            history.prior_contact_responses, history.prior_contacts_sent
        ),
        expiry_past=card_expiry_visibly_past(observation),
    )


def from_history_row(row: dict[str, Any]) -> Features:
    """Features for one logged action in ``history.jsonl``.

    The row schema is read structurally rather than imported from ``world/history.py``,
    for the same reason the observation schema redeclares its own enums: the boundary
    should hold even for a shape that happens to be identical.
    """
    return Features(
        intervention=str(row["intervention"]),
        rail=str(row["rail"]),
        error_code=str(row["error_code"]),
        attempt=attempt_bucket(int(row["attempt_index"])),
        contact=contact_bucket(int(row["contact_index"])),
        segment=str(row["segment_label"]),
        salary=salary_bucket(int(row["days_to_salary"])),
        late=late_bucket(float(row["avg_days_late"])),
        responsiveness=responsiveness_bucket(
            int(row["prior_contact_responses"]),
            int(row["prior_contacts_sent"]),
        ),
        expiry_past=bool(row["card_expiry_visibly_past"]),
    )


#: Backoff ladder, most specific first. Each level shrinks toward the next.
#:
#: The ladder exists because the full cross-product has far more cells than the log has
#: rows. Rather than pick one resolution and live with either bias or noise, the estimator
#: uses every resolution and lets the data decide how far down it can support.
BACKOFF_LEVELS: tuple[tuple[str, ...], ...] = (
    ("intervention", "error_code", "attempt", "salary", "late", "responsiveness",
     "expiry_past", "rail", "segment"),
    ("intervention", "error_code", "attempt", "salary", "late", "expiry_past"),
    ("intervention", "error_code", "attempt"),
    ("intervention", "error_code"),
    ("intervention",),
    (),
)
