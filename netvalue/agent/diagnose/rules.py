"""The rules arm — the ablation floor, written to be genuinely good.

This is what a competent payments engineer produces in an afternoon: a table keyed on the
gateway code's documented meaning, then adjusted by the obvious customer-history signals.
It costs nothing to run, never fails, and is completely deterministic.

**It is deliberately not a strawman.** If the LLM arm only beat a bad rule table, the
"AI judgment" claim would be worth nothing — the honest comparison is against the best
thing you could write without a model. So this uses every observable cue the world
actually puts there: a visibly passed expiry date, a customer who pays late, contacts that
were never answered, a subscription that stopped succeeding months ago, and the rail's own
structural constraints.

What it cannot do is weigh those cues *against each other* when they conflict — a late
payer whose card also shows expired, on a code that covers three causes. That conflict is
where a model earns its place, and Phase 8's ablation will say by how much in rupees.

Every weight below is a hand-written prior over documented code meanings. None is read
from the world's ``P(code | cause)`` matrix, which the agent cannot see.

**But be honest about the advantage.** They were written after that world was designed, by
the same person, so they land closer to the true posteriors than an outsider working from
the documented meanings alone would manage. A real payments engineer has genuinely seen
their own decline distribution, so the arm is realistic rather than cheating — but the
model arm gets no equivalent prior, and a narrow rules win is therefore not evidence that
rules beat models. Say so beside the result.
"""

from __future__ import annotations

from netvalue.agent.diagnose.schema import CausePosterior, DiagnosedCause
from netvalue.agent.observation import Observation

_C = DiagnosedCause

#: Base weights from what each code is *documented* to mean. A real engineer writes this
#: from the gateway's error reference, not from a calibrated table.
_CODE_PRIOR: dict[str, dict[DiagnosedCause, float]] = {
    # "Declined by issuer, reason unspecified" - the catch-all. Funds are the single
    # commonest cause of a bare issuer decline, but it covers risk and instrument too.
    "GW_05": {_C.INSUFFICIENT_FUNDS: 0.55, _C.RISK_BLOCK: 0.15, _C.CARD_EXPIRED: 0.10,
              _C.MANDATE_DEAD: 0.07, _C.AFA_TIMEOUT: 0.05, _C.BANK_OUTAGE: 0.04,
              _C.ROUTE_DEGRADED: 0.04},
    # "Do not honour" is the classic risk/fraud refusal, but issuers also return it for
    # a balance shortfall.
    "GW_11": {_C.INSUFFICIENT_FUNDS: 0.40, _C.RISK_BLOCK: 0.40, _C.MANDATE_DEAD: 0.08,
              _C.CARD_EXPIRED: 0.05, _C.AFA_TIMEOUT: 0.03, _C.BANK_OUTAGE: 0.02,
              _C.ROUTE_DEGRADED: 0.02},
    # "Instrument invalid" - the instrument is unusable. Either it expired or the mandate
    # is gone. Distinguishing those is the single most expensive call in the world.
    "GW_21": {_C.CARD_EXPIRED: 0.45, _C.MANDATE_DEAD: 0.40, _C.RISK_BLOCK: 0.06,
              _C.INSUFFICIENT_FUNDS: 0.05, _C.AFA_TIMEOUT: 0.02, _C.BANK_OUTAGE: 0.01,
              _C.ROUTE_DEGRADED: 0.01},
    # "Authorisation not completed" - the customer did not finish, or opted out.
    "GW_33": {_C.AFA_TIMEOUT: 0.65, _C.INSUFFICIENT_FUNDS: 0.13, _C.RISK_BLOCK: 0.10,
              _C.MANDATE_DEAD: 0.05, _C.BANK_OUTAGE: 0.04, _C.ROUTE_DEGRADED: 0.02,
              _C.CARD_EXPIRED: 0.01},
    # A timeout is infrastructure: either the issuer or the route between us and it.
    "GW_54": {_C.ROUTE_DEGRADED: 0.35, _C.BANK_OUTAGE: 0.33, _C.AFA_TIMEOUT: 0.13,
              _C.INSUFFICIENT_FUNDS: 0.10, _C.RISK_BLOCK: 0.04, _C.CARD_EXPIRED: 0.03,
              _C.MANDATE_DEAD: 0.02},
    # "Issuer unavailable" points hard at the bank, though issuers also emit it under load
    # when the real problem is elsewhere.
    "GW_91": {_C.BANK_OUTAGE: 0.50, _C.INSUFFICIENT_FUNDS: 0.20, _C.ROUTE_DEGRADED: 0.17,
              _C.CARD_EXPIRED: 0.05, _C.MANDATE_DEAD: 0.04, _C.RISK_BLOCK: 0.03,
              _C.AFA_TIMEOUT: 0.01},
}

#: An unmapped code carries no information, so fall back to a broad prior over what
#: actually fails. Guessing confidently from a code with no documented meaning is exactly
#: the failure the held-out regime is built to expose.
_UNKNOWN_CODE_PRIOR: dict[DiagnosedCause, float] = {
    _C.INSUFFICIENT_FUNDS: 0.34, _C.AFA_TIMEOUT: 0.16, _C.CARD_EXPIRED: 0.12,
    _C.RISK_BLOCK: 0.11, _C.BANK_OUTAGE: 0.12, _C.ROUTE_DEGRADED: 0.08,
    _C.MANDATE_DEAD: 0.07,
}

# --- multipliers applied to the base weights when a cue is present. [hand-written] ------

_EXPIRY_PASSED_BOOST = 4.0      # stored expiry is in the past
_EXPIRY_VALID_DAMPEN = 0.45     # stored expiry looks fine, so expiry is less likely
_LATE_PAYER_BOOST = 2.2         # habitually pays late
_PROMPT_PAYER_DAMPEN = 0.55     # always pays on time, so a balance shortfall is unlikely
_SILENT_CUSTOMER_BOOST = 2.0    # contacted before, never answered
_RESPONSIVE_DAMPEN = 0.5        # has answered before
_LONG_DEAD_BOOST = 3.0          # no successful debit in months
_RECENT_SUCCESS_DAMPEN = 0.35   # succeeded recently, so the mandate is alive
_PAYDAY_DAMPEN = 0.7            # money just landed, so a shortfall is less likely
_TROUGH_BOOST = 1.4             # deep mid-cycle, so a shortfall is more likely
_CLEAN_RECORD_BOOST = 1.5       # no customer-side signal at all points at infrastructure

_LATE_DAYS = 3.0
_DEAD_DAYS = 60
_RECENT_DAYS = 40
_PAYDAY_DAYS = 2
_TROUGH_DAYS = 5


class RulesDiagnoser:
    """Deterministic, free, and a fair opponent."""

    name = "rules"

    def diagnose(self, observation: Observation) -> CausePosterior:
        from netvalue.agent.features import days_to_nearest_salary_day

        code = observation.error_code.value
        weights = dict(_CODE_PRIOR.get(code, _UNKNOWN_CODE_PRIOR))
        h = observation.customer_history
        notes: list[str] = [f"{code} base prior"]

        # --- instrument -------------------------------------------------------------
        expired_on_file = (
            observation.card_exp_year is not None
            and observation.card_exp_month is not None
            and (observation.card_exp_year, observation.card_exp_month)
            < (observation.observed_at.year, observation.observed_at.month)
        )
        if expired_on_file:
            weights[_C.CARD_EXPIRED] *= _EXPIRY_PASSED_BOOST
            notes.append("stored expiry has passed")
        elif observation.is_card:
            weights[_C.CARD_EXPIRED] *= _EXPIRY_VALID_DAMPEN
            notes.append("stored expiry still valid")

        # --- ability to pay ----------------------------------------------------------
        if h.avg_days_late >= _LATE_DAYS:
            weights[_C.INSUFFICIENT_FUNDS] *= _LATE_PAYER_BOOST
            notes.append(f"pays {h.avg_days_late:.1f}d late on average")
        else:
            weights[_C.INSUFFICIENT_FUNDS] *= _PROMPT_PAYER_DAMPEN
            notes.append("pays promptly")

        days_to_payday = days_to_nearest_salary_day(observation.observed_at)
        if days_to_payday <= _PAYDAY_DAYS:
            weights[_C.INSUFFICIENT_FUNDS] *= _PAYDAY_DAMPEN
            notes.append("near payday")
        elif days_to_payday >= _TROUGH_DAYS:
            weights[_C.INSUFFICIENT_FUNDS] *= _TROUGH_BOOST
            notes.append("mid-cycle trough")

        # --- is the mandate alive at all? --------------------------------------------
        if h.last_success_at is None:
            weights[_C.MANDATE_DEAD] *= _LONG_DEAD_BOOST
            notes.append("no successful debit on record")
        else:
            days_since = (observation.observed_at - h.last_success_at).days
            if days_since >= _DEAD_DAYS:
                weights[_C.MANDATE_DEAD] *= _LONG_DEAD_BOOST
                notes.append(f"last success {days_since}d ago")
            elif days_since <= _RECENT_DAYS:
                weights[_C.MANDATE_DEAD] *= _RECENT_SUCCESS_DAMPEN
                notes.append(f"succeeded {days_since}d ago")

        # --- will they respond? -------------------------------------------------------
        if h.prior_contacts_sent > 0 and h.prior_contact_responses == 0:
            weights[_C.AFA_TIMEOUT] *= _SILENT_CUSTOMER_BOOST
            weights[_C.MANDATE_DEAD] *= _SILENT_CUSTOMER_BOOST
            notes.append("never answers outreach")
        elif h.prior_contact_responses > 0:
            weights[_C.AFA_TIMEOUT] *= _RESPONSIVE_DAMPEN
            notes.append("has answered outreach before")

        # --- absence of customer-side signal is itself a signal -----------------------
        clean = (
            h.avg_days_late < _LATE_DAYS
            and h.failed_debits_12m <= 1
            and not expired_on_file
        )
        if clean:
            weights[_C.BANK_OUTAGE] *= _CLEAN_RECORD_BOOST
            weights[_C.ROUTE_DEGRADED] *= _CLEAN_RECORD_BOOST
            notes.append("clean payment record points away from the customer")

        posterior = CausePosterior.from_weights(
            weights, rationale="; ".join(notes), source=self.name
        )
        return posterior.restricted_to_rail(observation.rail)
