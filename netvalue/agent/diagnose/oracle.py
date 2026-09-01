"""The oracle arm — perfect diagnosis, for the ceiling of the ablation.

Answers the question Phase 8 needs and cannot otherwise obtain: **how much of the gap to
the max-recovery ceiling is diagnosis, and how much is economics?** Running the full value
engine on a perfect diagnoser isolates the first from the second. If the oracle arm and
the LLM arm produce nearly the same net value, better diagnosis is not where the remaining
money is, and saying so is worth more than a bigger headline number.

**It imports nothing from ``netvalue.world``.** Ground truth arrives as plain data, handed
in by the runner, exactly as the oracle baselines receive it. That keeps this file off the
boundary allowlist: the guard stays absolute over ``agent/``, with no exceptions to
maintain or quietly widen.

The optional ``accuracy`` knob degrades the oracle toward the truth-free prior, which makes
it possible to ask "what would a diagnoser that is right 80% of the time be worth?" without
building one.
"""

from __future__ import annotations

from netvalue.agent.diagnose.schema import CausePosterior, DiagnosedCause
from netvalue.agent.observation import Observation


class OracleDiagnoser:
    """Returns the true cause. Never given to anything that is being measured fairly."""

    name = "oracle"

    def __init__(self, truth: dict[str, str], accuracy: float = 1.0) -> None:
        """``truth`` maps transaction id to the cause name; ``accuracy`` is the mass placed
        on it, with the remainder spread uniformly over the rest."""
        if not 0.0 < accuracy <= 1.0:
            raise ValueError("accuracy must be in (0, 1]")
        self.truth = {k: DiagnosedCause(v) for k, v in truth.items()}
        self.accuracy = accuracy

    def diagnose(self, observation: Observation) -> CausePosterior:
        cause = self.truth.get(observation.transaction_id)
        if cause is None:
            # Refusing to guess is the honest behaviour: a silent fallback to a plausible
            # posterior would make a broken truth map look like a working oracle.
            raise KeyError(
                f"no ground truth for {observation.transaction_id}; the oracle arm was "
                f"given an incomplete truth map"
            )

        if self.accuracy >= 1.0:
            return CausePosterior.point_mass(
                cause, rationale="ground truth", source=self.name
            )

        spread = (1.0 - self.accuracy) / (len(DiagnosedCause) - 1)
        return CausePosterior(
            probabilities={
                c: (self.accuracy if c is cause else spread) for c in DiagnosedCause
            },
            rationale=f"ground truth, degraded to {self.accuracy:.0%}",
            source=self.name,
        )


def truth_map(rows: list[dict[str, object]]) -> dict[str, str]:
    """Build the id -> cause map from dataset rows. Called by the runner, in ``eval/``."""
    return {str(r["transaction_id"]): str(r["true_cause"]) for r in rows}
