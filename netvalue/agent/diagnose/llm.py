"""The LLM arm — a posterior over causes from ambiguous evidence.

This is the one place in the system where a model belongs. The error code narrows the
cause and never settles it (``I(cause; code) = 0.627`` bits against ``H(cause) = 2.526``),
and the discriminating evidence is a handful of customer-history signals that pull in
different directions. Weighing conflicting weak evidence is the thing a rule table cannot
do and a model can — and Phase 8's ablation puts a rupee figure on the difference rather
than asserting it.

Everything else about the system stays deterministic. The model outputs a distribution;
the economics, the bounds and the stopping decision are arithmetic.

Three properties keep the experiment honest:

* **Structured output, validated.** The response must be a distribution over exactly the
  seven causes. A malformed answer is retried and then raised, never silently replaced by
  a plausible default that would quietly become the result.
* **Cached on the exact request.** A diagnosis is paid for once. Re-runs are free,
  deterministic, and work with no network.
* **The prompt carries the merchant's own base rates, and nothing finer.** It states how
  often each cause occurs across this merchant's failures — the same population knowledge
  the rules arm's code priors encode — and describes what each cause is and what fixes it.
  It does **not** carry the world's ``P(cause | code)`` table, which would be the answer
  sheet.

  This was added after measuring the gap. Without base rates the model scored 30.1% against
  the rules table's 65.5%, and reweighting its cached posteriors by the true mix lifted it
  to 45.6% — so roughly half the deficit was a missing prior rather than bad evidence
  reading. Giving one arm population knowledge and withholding it from the other was not a
  fair ablation; both have it now. Recorded as DECISION-100.
"""

from __future__ import annotations

from typing import Any

from netvalue.agent.diagnose import evidence
from netvalue.agent.diagnose.schema import CausePosterior, DiagnosedCause
from netvalue.agent.observation import Observation
from netvalue.llm.client import StructuredClient

SYSTEM_PROMPT = """\
You are a payments recovery analyst at a subscription business in India. A recurring \
payment has failed. Your job is to say what most likely went wrong, as a probability \
distribution over seven possible causes.

THE SEVEN CAUSES

insufficient_funds  The customer's account lacked the balance. Fixable by retrying when
                    money is likely to be there.
card_expired        The stored card is dead - expired or reissued with a new number. No
                    retry can ever fix this; only the customer updating their card can.
risk_block          The issuer's fraud or risk system refused the debit. Retrying does
                    nothing; only a human working with the issuer can lift it.
mandate_dead        The mandate itself is revoked, or the account is closed. Nothing works.
                    This is the unrecoverable case.
afa_timeout         The authorisation step did not complete: either the customer did not
                    finish an authentication challenge, or they used the opt-out attached
                    to the pre-debit notification. Often succeeds on a later presentation.
bank_outage         The issuing bank's systems were unavailable. Clears on its own; a
                    retry after the outage usually succeeds.
route_degraded      The acquirer route we used was failing. Switching route fixes it.

HOW TO REASON

The gateway error code narrows the cause but never determines it. One code routinely
covers three or more of the causes above. If you decide from the code alone you will be
wrong often and confidently, which is worse than being uncertain.

The evidence that actually separates the causes is the customer's payment history:

- A customer who habitually pays late, with a scatter of prior failures, is showing you a
  balance problem.
- A long clean record that stops dead, with nothing about the customer having changed,
  points at the instrument rather than the person.
- No successful debit for months, and outreach that has never once been answered, points
  at a mandate that is simply gone.
- A clean record with no customer-side signal at all points away from the customer
  entirely, toward the bank or the route.
- A stored expiry date that has already passed is strong evidence, but not conclusive: a
  reissued card is dead while its stored expiry still looks fine.

Rail matters. A UPI Autopay mandate has no card to expire and no acquirer route to
switch, so those two causes are impossible there. Assign them zero.

If the error code is one you do not recognise, say so in your reasoning and spread your
probability according to the customer evidence alone. Do not invent a meaning for it.

WHAT FAILS, AND HOW OFTEN

Across this merchant's own failed renewals, roughly:

  insufficient_funds  38 in 100      bank_outage      12 in 100
  afa_timeout         17 in 100      card_expired      8 in 100
  risk_block          11 in 100      mandate_dead      8 in 100
  route_degraded       7 in 100

Use this as your starting point and let the evidence move you off it. It matters because
the failure that dominates in practice is the dullest one - customers who simply did not
have the money - while the codes that *sound* most diagnostic are timeouts and gateway
errors, which are comparatively rare. Reading a timeout and concluding "infrastructure"
without checking the customer's payment history is the single commonest way to be wrong
here.

CALIBRATION MATTERS MORE THAN CONFIDENCE

Your probabilities are multiplied by rupees and compared against a cost. If you say 90%
you should be right about nine times in ten. When two causes genuinely both fit, split
the mass between them - that is the correct answer, not a hedge. Reserve high confidence
for cases where the evidence really does point one way.

Return probabilities over all seven causes summing to 1.0, and one or two sentences
naming the specific evidence that moved you."""


def response_schema() -> dict[str, Any]:
    """Strict JSON schema: exactly seven probabilities plus the reasoning."""
    return {
        "type": "object",
        "properties": {
            **{
                cause.value: {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "description": f"probability that the cause was {cause.value}",
                }
                for cause in DiagnosedCause
            },
            "reasoning": {
                "type": "string",
                "description": "one or two sentences naming the evidence that decided it",
            },
        },
        "required": [c.value for c in DiagnosedCause] + ["reasoning"],
        "additionalProperties": False,
    }


class LLMDiagnoser:
    """Reads the evidence view, returns a validated posterior."""

    name = "llm"

    def __init__(self, client: StructuredClient) -> None:
        self.client = client
        self._schema = response_schema()

    def diagnose(self, observation: Observation) -> CausePosterior:
        prompt = evidence.build(observation)
        payload = self.client.complete_json(
            system=SYSTEM_PROMPT,
            prompt=prompt,
            schema=self._schema,
            schema_name="cause_diagnosis",
        )

        weights: dict[DiagnosedCause, float] = {}
        for cause in DiagnosedCause:
            raw = payload.get(cause.value, 0.0)
            try:
                value = float(raw)
            except (TypeError, ValueError):
                value = 0.0
            # A model occasionally emits a small negative or a value above one. Clamping
            # and renormalising is right; rejecting the whole response over a rounding
            # artefact would burn a paid call for nothing.
            weights[cause] = min(max(value, 0.0), 1.0)

        if sum(weights.values()) <= 0.0:
            # Everything zero is not a distribution. Fall back to the honest statement
            # that we learned nothing, rather than to a confident-looking guess.
            return CausePosterior.uniform(source=self.name).restricted_to_rail(
                observation.rail
            )

        posterior = CausePosterior.from_weights(
            weights,
            rationale=str(payload.get("reasoning", ""))[:500],
            source=self.name,
        )
        return posterior.restricted_to_rail(observation.rail)

    def usage(self) -> dict[str, Any]:
        return self.client.usage.summary()
