"""The error code must not give the cause away.

If ``GW_05`` mapped cleanly to ``insufficient_funds`` there would be no inference problem,
no reason for a model in the system, and a reviewer would say so in ten seconds. The
project's premise requires the error code to be genuinely one-to-many, and requires
customer history to carry the rest of the signal.

Phase 0 designed the ``P(code | cause)`` matrix to satisfy that. This module asserts it,
so the constraint is mechanical from the first commit rather than checked once by hand.
The margins are thin on purpose: if the world drifts, these fail.
"""

from __future__ import annotations

import math

import pytest

from netvalue.world.config import CONFIG_A, Cause, ErrorCode, WorldConfig


@pytest.fixture(scope="module")
def cfg() -> WorldConfig:
    return CONFIG_A


def test_every_code_is_reachable(cfg: WorldConfig) -> None:
    marginal = cfg.code_marginal()
    assert math.isclose(math.fsum(marginal.values()), 1.0, abs_tol=1e-9)
    for code, p in marginal.items():
        assert p > 0.01, f"{code} is effectively unreachable (P={p:.5f})"


def test_posteriors_are_distributions(cfg: WorldConfig) -> None:
    for code, dist in cfg.posterior_cause_given_code().items():
        assert math.isclose(math.fsum(dist.values()), 1.0, abs_tol=1e-9), code


def test_no_error_code_concentrates_on_one_cause(cfg: WorldConfig) -> None:
    """The core constraint: the code narrows the cause but never resolves it."""
    ceiling = cfg.ambiguity.max_posterior_mass_per_code
    posterior = cfg.posterior_cause_given_code()

    offenders = {
        code: max(dist.items(), key=lambda kv: kv[1])
        for code, dist in posterior.items()
        if max(dist.values()) > ceiling
    }
    assert not offenders, (
        f"error codes concentrate above {ceiling:.0%}: "
        + ", ".join(f"{c}->{cause} at {p:.1%}" for c, (cause, p) in offenders.items())
        + ". Spread mass to other causes, or the diagnosis step has nothing to do."
    )


def test_conditional_entropy_floor(cfg: WorldConfig) -> None:
    """Even the most informative code must leave real uncertainty behind."""
    floor = cfg.ambiguity.min_conditional_entropy_bits
    per_code = cfg.conditional_entropy_by_code_bits()
    offenders = {c: h for c, h in per_code.items() if h < floor}
    assert not offenders, (
        f"H(cause | code) below {floor} bits for: "
        + ", ".join(f"{c}={h:.3f}" for c, h in offenders.items())
    )


def test_mutual_information_ceiling(cfg: WorldConfig) -> None:
    """Across the whole table, the code must not carry most of the answer."""
    mi = cfg.mutual_information_bits()
    ceiling = cfg.ambiguity.max_mutual_information_bits
    assert 0.0 < mi <= ceiling, (
        f"I(cause; code) = {mi:.3f} bits, ceiling {ceiling}. "
        f"H(cause) = {cfg.cause_entropy_bits():.3f}, "
        f"H(cause | code) = {cfg.conditional_entropy_bits():.3f}."
    )


def test_at_least_three_codes_are_genuinely_ambiguous(cfg: WorldConfig) -> None:
    """Phase 0 requires at least three codes with real mass on two or more causes.

    'Real mass' means a runner-up above 20%: enough that acting on the leading cause
    without consulting history is a materially wrong decision, not a rounding error.
    """
    posterior = cfg.posterior_cause_given_code()
    ambiguous: list[ErrorCode] = []
    for code, dist in posterior.items():
        ranked = sorted(dist.values(), reverse=True)
        if ranked[1] > 0.20:
            ambiguous.append(code)
    assert len(ambiguous) >= 3, (
        f"only {len(ambiguous)} genuinely ambiguous codes ({ambiguous}); Phase 0 requires 3+"
    )


def test_the_three_economically_loaded_ambiguities_survive(cfg: WorldConfig) -> None:
    """These three are the ones where the wrong call is expensive, not merely wrong.

    GW_11  insufficient_funds vs risk_block  -> timed retry vs human escalation
    GW_21  card_expired vs mandate_dead      -> a paid contact vs abandoning
    GW_54  route_degraded vs bank_outage     -> switch the route vs wait it out

    Which of the pair leads is deliberately **not** asserted. Ordering is an artifact of
    the rail mix — ``card_expired`` is card-only, so it carries less effective mass than
    ``mandate_dead`` even though it is more likely conditional on the card rail — and it
    flips under small, legitimate changes. What must hold is that both members carry real
    mass, so acting on the leader without consulting history is a materially wrong call.
    """
    expected: dict[ErrorCode, set[Cause]] = {
        ErrorCode.GW_11: {Cause.INSUFFICIENT_FUNDS, Cause.RISK_BLOCK},
        ErrorCode.GW_21: {Cause.CARD_EXPIRED, Cause.MANDATE_DEAD},
        ErrorCode.GW_54: {Cause.ROUTE_DEGRADED, Cause.BANK_OUTAGE},
    }
    posterior = cfg.posterior_cause_given_code()

    for code, pair in expected.items():
        ranked = sorted(posterior[code].items(), key=lambda kv: kv[1], reverse=True)
        top_two = {cause for cause, _ in ranked[:2]}
        assert top_two == pair, f"{code} should be contested by {pair}, got {top_two}"
        assert ranked[1][1] > 0.20, (
            f"{code}: runner-up {ranked[1][0]} at {ranked[1][1]:.1%} is too weak to force "
            "the agent to consult customer history"
        )


def test_permanently_unrecoverable_cause_is_not_trivially_identifiable(
    cfg: WorldConfig,
) -> None:
    """``mandate_dead`` must hide behind ``card_expired``.

    If the agent could spot a dead mandate from the code alone it would abandon perfectly
    and for free, and the interesting half of the decision problem would disappear.
    """
    posterior = cfg.posterior_cause_given_code()
    best = max(dist[Cause.MANDATE_DEAD] for dist in posterior.values())
    assert best < 0.50, (
        f"mandate_dead reaches {best:.1%} posterior mass on some code; "
        "it must stay confusable with card_expired"
    )
