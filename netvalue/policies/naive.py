"""Baseline 2 — fixed retry. What most production dunning systems actually do.

Three attempts, 24 hours apart, regardless of what failed or what the customer is worth.
It never contacts anyone, so it incurs no annoyance cost at all — which makes it a
genuinely strong competitor on net value and a much fairer opponent than a strawman.

The hardcoded ``max_attempts = 3`` is the laziest line in every competing entry, and
reproducing it faithfully here is the point: the submission has to beat this on its own
terms, not against a version of it built to lose.
"""

from __future__ import annotations

from netvalue.agent.observation import Observation
from netvalue.agent.policy import Action, ActionKind


class NaiveRetryPolicy:
    """Fixed schedule. No diagnosis, no economics, no stopping rule."""

    name = "naive_retry"

    def __init__(self, max_attempts: int = 3, interval_hours: float = 24.0) -> None:
        self.max_attempts = max_attempts
        self.interval_hours = interval_hours

    def decide(self, observation: Observation) -> Action:
        if observation.attempt_number > self.max_attempts:
            return Action(
                kind=ActionKind.ABANDON,
                rationale=f"fixed cap of {self.max_attempts} attempts reached",
            )
        return Action(
            kind=ActionKind.RETRY_AFTER,
            delay_hours=self.interval_hours,
            rationale=f"fixed schedule: attempt {observation.attempt_number}",
        )

    def reset(self) -> None:
        return None
