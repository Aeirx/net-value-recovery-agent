"""Turning an observation into the compact view a model reasons over.

The build plan is blunt about this module: *when the agent underperforms, the bug is
almost always in the evidence view, not the prompt.* An LLM cannot infer from a payment
history it was never shown, and the temptation is to fix a weak diagnosis by rewriting
instructions when the actual fault is that the discriminating field never made it into the
context.

So the view is built to carry exactly the signals the world puts there. Phase 3 generated
customer history **cause-conditionally** — late payers look like insufficient funds, clean
records that stop dead look like an expired card, unanswered contacts look like a dead
mandate — and every one of those cues appears below, stated plainly rather than left for
the model to derive from raw timestamps.

Two rules:

* **Facts, never hints.** The view says "pays 6.4 days late on average", not "this looks
  like insufficient funds". A view that names the answer is not evidence, it is a leak
  through a slower channel, and it would make the LLM arm look good for the wrong reason.
* **Derived quantities are stated when a human would compute them.** Days until the
  mandate expires and distance to payday are arithmetic anybody does; making the model
  recover them from ISO timestamps wastes its attention on the wrong problem.
"""

from __future__ import annotations

from netvalue.agent.features import days_to_nearest_salary_day
from netvalue.agent.observation import Observation, ObservedRail

#: What each gateway code is documented to mean. Note that none of these names a cause:
#: that is the design constraint the whole project rests on, restated where the model can
#: see it, so it knows the code narrows the answer without settling it.
CODE_MEANINGS: dict[str, str] = {
    "GW_05": "declined by issuing bank, reason not specified",
    "GW_11": "do not honour - issuer refused authorisation",
    "GW_21": "payment instrument reported invalid or unusable",
    "GW_33": "authorisation step not completed",
    "GW_54": "timeout waiting for the issuer or acquirer",
    "GW_91": "issuer system unavailable",
    "GW_99": "unmapped processor response (no documented meaning)",
}


def _describe_expiry(observation: Observation) -> str:
    if observation.card_exp_year is None or observation.card_exp_month is None:
        return "not applicable (no card on this rail)"
    ref = observation.observed_at
    stored = (observation.card_exp_year, observation.card_exp_month)
    if stored < (ref.year, ref.month):
        return f"{observation.card_exp_month:02d}/{observation.card_exp_year} - ALREADY PASSED"
    return f"{observation.card_exp_month:02d}/{observation.card_exp_year} - still valid"


def _describe_contacts(observation: Observation) -> str:
    h = observation.customer_history
    if h.prior_contacts_sent == 0:
        return "never contacted before"
    if h.prior_contact_responses == 0:
        return f"{h.prior_contacts_sent} contact(s) sent, NONE answered"
    return f"{h.prior_contacts_sent} contact(s) sent, {h.prior_contact_responses} answered"


def _describe_last_success(observation: Observation) -> str:
    h = observation.customer_history
    if h.last_success_at is None:
        return "no successful debit on record"
    days = (observation.observed_at - h.last_success_at).days
    return f"{days} days ago"


def _describe_attempts(observation: Observation) -> str:
    if not observation.prior_attempts:
        return "none - this is the first recovery action"
    lines = []
    for i, a in enumerate(observation.prior_attempts, start=1):
        code = a.error_code.value if a.error_code else "-"
        lines.append(f"    {i}. {a.intervention} at {a.at:%d %b %H:%M} -> {a.outcome} ({code})")
    return "\n" + "\n".join(lines)


def build(observation: Observation) -> str:
    """The complete evidence view. Plain text, stable field order, no ground truth."""
    h = observation.customer_history
    rail = "card-on-file mandate" if observation.is_card else "UPI Autopay mandate"
    code = observation.error_code.value
    days_left = observation.hours_remaining / 24.0
    payday = days_to_nearest_salary_day(observation.observed_at)

    lines = [
        "FAILED RECURRING PAYMENT",
        f"  rail                 {rail}",
        f"  amount               Rs {observation.amount_inr:,.0f} per cycle",
        f"  subscription age     {observation.plan_tenure_months} months",
        "",
        "GATEWAY RESPONSE",
        f"  code                 {code} - {CODE_MEANINGS.get(code, 'unknown code')}",
        f'  message              "{observation.error_message}"',
        f"  bank / PSP           {observation.bank_id}",
        f"  stored card expiry   {_describe_expiry(observation)}",
        "",
        "CUSTOMER PAYMENT HISTORY (last 12 months)",
        f"  successful debits    {h.successful_debits_12m}",
        f"  failed debits        {h.failed_debits_12m}",
        f"  last successful      {_describe_last_success(observation)}",
        f"  average days late    {h.avg_days_late:.1f}",
        f"  outreach             {_describe_contacts(observation)}",
        f"  engagement segment   {h.segment_label.value}",
        "",
        "THIS RECOVERY ATTEMPT",
        f"  attempt number       {observation.attempt_number}",
        f"  debits this cycle    {observation.mandate_debits_this_cycle}",
        f"  prior actions        {_describe_attempts(observation)}",
        "",
        "TIMING",
        f"  now                  {observation.observed_at:%a %d %b %Y, %H:%M}",
        f"  days to payday       {payday} (salary credits cluster on the 1st and 7th)",
        f"  mandate expires in   {days_left:.1f} days",
    ]
    if observation.rail is ObservedRail.UPI_AUTOPAY:
        lines += [
            "",
            "RAIL CONSTRAINTS",
            "  This is a UPI Autopay mandate: there is no card to expire and no acquirer",
            "  route to switch, so card_expired and route_degraded are impossible here.",
        ]
    return "\n".join(lines)


def build_batch(observations: list[Observation]) -> list[str]:
    return [build(o) for o in observations]
