"""The diagnosis vocabulary and the shape every diagnoser returns.

The cause names are **redeclared here** rather than imported from ``world/config.py`` —
the same discipline as ``ObservedErrorCode``. Knowing that cards expire and mandates get
revoked is domain knowledge any payments engineer has; knowing which one *this* payment
hit is ground truth. The boundary holds even for the vocabulary.

Every diagnoser returns a **posterior over all seven causes**, never a label. Committing
to the argmax throws away exactly the ambiguity that justifies having a model in the
system: on ``GW_21`` the top two causes sit at 43% and 40%, and one implies a paid customer
contact while the other implies abandoning. A label cannot express that. A distribution can,
and the value engine can price it.
"""

from __future__ import annotations

import math
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from netvalue.agent.observation import Observation, ObservedRail

Probability = Annotated[float, Field(ge=0.0, le=1.0)]


class DiagnosedCause(StrEnum):
    """What the agent believes went wrong. Mirrors the world's taxonomy by name only."""

    INSUFFICIENT_FUNDS = "insufficient_funds"
    CARD_EXPIRED = "card_expired"
    RISK_BLOCK = "risk_block"
    MANDATE_DEAD = "mandate_dead"
    AFA_TIMEOUT = "afa_timeout"
    BANK_OUTAGE = "bank_outage"
    ROUTE_DEGRADED = "route_degraded"


#: Causes that cannot physically occur on a UPI Autopay mandate: there is no card to
#: expire and no acquirer to switch. Structural domain knowledge, not world state.
CARD_ONLY_CAUSES: frozenset[DiagnosedCause] = frozenset(
    {DiagnosedCause.CARD_EXPIRED, DiagnosedCause.ROUTE_DEGRADED}
)


class CausePosterior(BaseModel):
    """A distribution over causes, with the reasoning that produced it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    probabilities: dict[DiagnosedCause, Probability]
    rationale: str = ""
    #: Which diagnoser produced this. Carried into the audit log.
    source: str = ""

    @model_validator(mode="after")
    def _check_distribution(self) -> Self:
        if set(self.probabilities) != set(DiagnosedCause):
            missing = set(DiagnosedCause) - set(self.probabilities)
            raise ValueError(f"posterior must cover every cause; missing {missing}")
        total = math.fsum(self.probabilities.values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"posterior must sum to 1.0, got {total!r}")
        return self

    # ------------------------------------------------------------------ accessors

    def __getitem__(self, cause: DiagnosedCause) -> float:
        return self.probabilities[cause]

    @property
    def top(self) -> DiagnosedCause:
        return max(self.probabilities.items(), key=lambda kv: kv[1])[0]

    @property
    def confidence(self) -> float:
        """Mass on the leading cause. The number an escalation threshold is set against."""
        return max(self.probabilities.values())

    @property
    def entropy_bits(self) -> float:
        """How undecided the diagnosis is. Reported so a confident-but-wrong diagnoser and
        an honestly-uncertain one can be told apart."""
        return -math.fsum(
            p * math.log2(p) for p in self.probabilities.values() if p > 0.0
        )

    def ranked(self) -> list[tuple[DiagnosedCause, float]]:
        return sorted(self.probabilities.items(), key=lambda kv: kv[1], reverse=True)

    def mass_on(self, causes: frozenset[DiagnosedCause]) -> float:
        return math.fsum(self.probabilities[c] for c in causes)

    # ------------------------------------------------------------------ builders

    @classmethod
    def from_weights(
        cls,
        weights: dict[DiagnosedCause, float],
        *,
        rationale: str = "",
        source: str = "",
        floor: float = 1e-4,
    ) -> CausePosterior:
        """Normalise arbitrary non-negative weights into a valid posterior.

        ``floor`` keeps every cause at a hair above zero. A hard zero is a claim of
        certainty that a cause is impossible, and it is unrecoverable: no amount of later
        evidence can move a cause off zero in a Bayesian update. Reserve exact zeros for
        the genuinely impossible — see :meth:`restricted_to_rail`.
        """
        if any(w < 0.0 for w in weights.values()):
            raise ValueError("weights must be non-negative")
        filled = {c: max(float(weights.get(c, 0.0)), floor) for c in DiagnosedCause}
        total = math.fsum(filled.values())
        return cls(
            probabilities={c: w / total for c, w in filled.items()},
            rationale=rationale,
            source=source,
        )

    @classmethod
    def point_mass(
        cls, cause: DiagnosedCause, *, rationale: str = "", source: str = ""
    ) -> CausePosterior:
        return cls(
            probabilities={c: (1.0 if c is cause else 0.0) for c in DiagnosedCause},
            rationale=rationale,
            source=source,
        )

    @classmethod
    def uniform(cls, *, source: str = "") -> CausePosterior:
        n = len(DiagnosedCause)
        return cls(
            probabilities={c: 1.0 / n for c in DiagnosedCause},
            rationale="no information",
            source=source,
        )

    def restricted_to_rail(self, rail: ObservedRail) -> CausePosterior:
        """Zero out causes the rail cannot produce, and renormalise.

        These are true impossibilities rather than low probabilities, so exact zeros are
        correct here: a UPI Autopay mandate has no card to expire.
        """
        if rail is not ObservedRail.UPI_AUTOPAY:
            return self
        kept = {
            c: (0.0 if c in CARD_ONLY_CAUSES else p)
            for c, p in self.probabilities.items()
        }
        total = math.fsum(kept.values())
        if total <= 0.0:
            return CausePosterior.uniform(source=self.source).restricted_to_rail(rail)
        return CausePosterior(
            probabilities={c: p / total for c, p in kept.items()},
            rationale=self.rationale,
            source=self.source,
        )


def rail_admissible(rail: ObservedRail) -> frozenset[DiagnosedCause]:
    if rail is ObservedRail.UPI_AUTOPAY:
        return frozenset(set(DiagnosedCause) - set(CARD_ONLY_CAUSES))
    return frozenset(DiagnosedCause)


class Diagnoser:
    """Protocol every diagnosis arm implements. Kept identical across arms so the
    ablation compares diagnosers and nothing else."""

    name: str

    def diagnose(self, observation: Observation) -> CausePosterior:  # pragma: no cover
        raise NotImplementedError
