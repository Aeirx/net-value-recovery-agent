"""Ground-truth cause assignment and the ambiguous error code the agent gets to see.

This module is where the project's central constraint is actually enforced at generation
time: the cause is sampled first, and the error code is then drawn from ``P(code | cause)``
so that a single code genuinely covers several causes. Nothing downstream can recover the
cause from the code alone, because the information is not there —
``I(cause; code) = 0.705 bits`` against ``H(cause) = 2.608``.
"""

from __future__ import annotations

from netvalue.world import rng
from netvalue.world.config import (
    BASE_ERROR_CODES,
    Cause,
    ErrorCode,
    Rail,
    WorldConfig,
)

#: Free-text messages a gateway might attach to a code. Deliberately noisy: several
#: messages appear under more than one code, none names a cause, and the phrasing varies
#: the way real gateway text does. An agent that keyword-matches on these will do worse
#: than one that reasons from customer history, which is the intended lesson.
ERROR_MESSAGES: dict[ErrorCode, tuple[str, ...]] = {
    ErrorCode.GW_05: (
        "Transaction declined by issuing bank",
        "Issuer declined the debit request",
        "Do not honour - issuer",
        "Payment refused by card issuer",
    ),
    ErrorCode.GW_11: (
        "Do not honour",
        "Issuer refused authorisation",
        "Transaction not permitted at this time",
        "Declined - contact issuer",
    ),
    ErrorCode.GW_21: (
        "Invalid or unusable payment instrument",
        "Instrument cannot be debited",
        "Card or mandate not valid for this debit",
        "Payment instrument rejected",
    ),
    ErrorCode.GW_33: (
        "Authentication not completed",
        "Customer did not complete authorisation",
        "Mandate authorisation step failed",
        "Debit not authorised by customer",
    ),
    ErrorCode.GW_54: (
        "Upstream timeout",
        "No response from issuer within timeout",
        "Gateway timeout on authorisation",
        "Request timed out at acquirer",
    ),
    ErrorCode.GW_91: (
        "Issuer unavailable",
        "Issuing bank system not responding",
        "Bank endpoint unreachable",
        "Issuer down for maintenance",
    ),
    ErrorCode.GW_99: (
        "Unclassified processor response",
        "Unmapped decline code from acquirer",
    ),
}


def causes_for_rail(cfg: WorldConfig, rail: Rail) -> list[Cause]:
    """Causes that can physically occur on a rail.

    ``card_expired`` and ``route_degraded`` are card-only: UPI Autopay has no card to
    expire and no acquirer to switch. This makes intervention validity rail-dependent,
    which the agent has to respect.
    """
    return [c for c, spec in cfg.causes.items() if rail in spec.rails]


def sample_cause(cfg: WorldConfig, transaction_id: str, rail: Rail) -> Cause:
    """Draw the hidden cause, renormalising the priors over what the rail permits."""
    candidates = causes_for_rail(cfg, rail)
    weights = [cfg.causes[c].prior for c in candidates]
    return rng.choice(cfg.seed, candidates, weights, "cause", transaction_id)


def sample_error_code(cfg: WorldConfig, transaction_id: str, cause: Cause) -> ErrorCode:
    """Draw the observable code from ``P(code | cause)``.

    Under the held-out regime a share of codes are overwritten with ``GW_99``, which never
    appears in the tuning world. The overwrite happens *after* the honest draw, so the
    underlying cause distribution is unchanged and any degradation is attributable to the
    unseen label rather than to a shifted world.
    """
    if cfg.regime.unseen_code_share > 0.0 and rng.bernoulli(
        cfg.seed, cfg.regime.unseen_code_share, "unseen-code", transaction_id
    ):
        return ErrorCode.GW_99

    weights = [cfg.code_given_cause[cause][code] for code in BASE_ERROR_CODES]
    return rng.choice(cfg.seed, BASE_ERROR_CODES, weights, "code", transaction_id)


def sample_error_message(cfg: WorldConfig, transaction_id: str, code: ErrorCode) -> str:
    options = ERROR_MESSAGES[code]
    return rng.choice(
        cfg.seed, options, [1.0] * len(options), "message", transaction_id
    )
