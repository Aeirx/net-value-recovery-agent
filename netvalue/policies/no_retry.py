"""Baseline 1 — the floor.

Does nothing. Every failed payment is written off immediately.

It exists to make the other numbers legible: net value here is exactly zero, so any
policy that cannot beat it is actively destroying value rather than merely underperforming.
"""

from __future__ import annotations

from netvalue.agent.observation import Observation
from netvalue.agent.policy import Action, ActionKind


class NoRetryPolicy:
    name = "no_retry"

    def decide(self, observation: Observation) -> Action:
        return Action(
            kind=ActionKind.ABANDON,
            expected_value_inr=0.0,
            rationale="baseline: never attempts recovery",
        )

    def reset(self) -> None:
        return None
