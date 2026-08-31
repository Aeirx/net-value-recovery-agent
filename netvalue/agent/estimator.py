"""P-hat with uncertainty, fitted on history.jsonl. The agent's only source of physics.

Phase 5 deliverable. Present in Phase 1 so the package shape — which encodes the
world/agent boundary — is real and testable from the first commit.
"""

from __future__ import annotations

_PHASE = 5


def _not_yet(what: str) -> None:
    raise NotImplementedError(f"{what} lands in Phase {_PHASE}")
