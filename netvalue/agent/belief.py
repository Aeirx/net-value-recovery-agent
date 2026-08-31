"""Belief state over causes, Bayesian-updated on every failed attempt.

Phase 7 deliverable. Present in Phase 1 so the package shape — which encodes the
world/agent boundary — is real and testable from the first commit.
"""

from __future__ import annotations

_PHASE = 7


def _not_yet(what: str) -> None:
    raise NotImplementedError(f"{what} lands in Phase {_PHASE}")
