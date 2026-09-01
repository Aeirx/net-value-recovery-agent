"""Transaction generation.

The design point that matters most in this module is **where the signal lives**.

The error code is deliberately uninformative — one code covers several causes, by
construction. If nothing else distinguished them, diagnosis would be guessing from the
prior and there would be no reason for a model in the system. So the discriminating
evidence is placed in *customer history*, cause-conditionally:

* ``insufficient_funds`` — pays, but pays late. High ``avg_days_late``, a scatter of prior
  failures, and a recent success.
* ``card_expired`` — a long clean record that stops dead. Sometimes, but not always, a
  stored expiry date that has passed.
* ``mandate_dead`` — no recent success at all, and an old mandate.
* ``risk_block`` — a newer mandate and a larger-than-usual debit.
* ``afa_timeout`` — a record of contacts sent and not answered.
* ``bank_outage`` / ``route_degraded`` — a clean record. It was never the customer's fault,
  and that *absence* of customer-side signal is itself the tell.

An agent that reasons from history beats one that keyword-matches the error message. That
gap is the whole justification for the diagnosis layer, and it is created here.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from netvalue.world import rng
from netvalue.world.banks import sample_bank, sample_route
from netvalue.world.calendar import fy_end_stress
from netvalue.world.causes import sample_cause, sample_error_code, sample_error_message
from netvalue.world.config import (
    Cause,
    ErrorCode,
    PlanTier,
    Rail,
    Segment,
    WorldConfig,
)

CARD_NETWORKS: tuple[str, ...] = ("VISA", "MASTERCARD", "RUPAY")

#: Share of expired-card failures where the *stored* expiry has visibly passed. The rest
#: are reissues: the card was replaced and the old number is dead while the stored expiry
#: still looks valid. Without this, the card-expiry field would resolve GW_21 outright and
#: the most economically interesting ambiguity in the world would collapse. [chosen]
_EXPIRED_VISIBLE_SHARE = 0.55

#: Share of dead mandates that nonetheless carry a plausible future expiry date. [chosen]
_DEAD_LOOKS_VALID_SHARE = 0.90


class CustomerHistoryRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    successful_debits_12m: int
    failed_debits_12m: int
    last_success_at: datetime | None
    avg_days_late: float
    prior_contact_responses: int
    prior_contacts_sent: int
    segment_label: Segment


class Transaction(BaseModel):
    """A generated failed payment, ground truth included.

    ``true_cause`` is the hidden field. Everything else is observable, and
    ``netvalue.agent.observation.Observation`` is the projection the agent actually sees.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    transaction_id: str
    merchant_id: str
    customer_id: str

    rail: Rail
    amount_inr: float
    plan_name: str
    plan_tenure_months: int

    error_code: ErrorCode
    error_message: str
    bank_id: str

    card_last4: str | None
    card_network: str | None
    card_exp_month: int | None
    card_exp_year: int | None

    mandate_id: str
    mandate_created_at: datetime
    mandate_debits_this_cycle: int
    acquirer_route: str | None

    customer_history: CustomerHistoryRecord
    first_failure_at: datetime
    expires_at: datetime

    # --- hidden ---------------------------------------------------------------------
    true_cause: Cause
    segment: Segment

    def public_fields(self) -> dict[str, object]:
        """Everything except ground truth. What ``history.jsonl`` is allowed to contain."""
        data = self.model_dump(mode="json")
        data.pop("true_cause")
        return data


def _sample_plan(cfg: WorldConfig, txn_id: str) -> PlanTier:
    return rng.choice(
        cfg.seed, list(cfg.plans), [p.share for p in cfg.plans], "plan", txn_id
    )


def _sample_segment(cfg: WorldConfig, txn_id: str) -> Segment:
    items = list(cfg.segments.items())
    return rng.choice(
        cfg.seed, [s for s, _ in items], [spec.share for _, spec in items],
        "segment", txn_id,
    )


def _sample_rail(cfg: WorldConfig, txn_id: str) -> Rail:
    items = list(cfg.rail_shares.items())
    return rng.choice(
        cfg.seed, [r for r, _ in items], [w for _, w in items], "rail", txn_id
    )


def _failure_time(cfg: WorldConfig, txn_id: str) -> datetime:
    """Draw a failure instant, with rejection sampling that biases toward year-end.

    Failures are not uniform across the month: the financial-year-end close concentrates
    them. Sampling uniformly and then accepting proportional to the stress multiplier
    produces that clustering without hand-placing a spike.
    """
    span_h = (cfg.clock.end - cfg.clock.start).total_seconds() / 3600.0
    # Leave room for the mandate horizon so most transactions can play out in-window.
    usable_h = max(span_h - 24.0 * 22.0, span_h * 0.35)
    peak = max(fy_end_stress(cfg.clock.start + timedelta(hours=h)) for h in (0.0, usable_h))

    for attempt in range(24):
        offset = rng.uniform(cfg.seed, 0.0, usable_h, "fail-time", txn_id, attempt)
        when = cfg.clock.start + timedelta(hours=offset)
        accept = fy_end_stress(when) / max(peak, 1.0)
        if rng.bernoulli(cfg.seed, accept, "fail-accept", txn_id, attempt):
            return when
    return cfg.clock.start + timedelta(
        hours=rng.uniform(cfg.seed, 0.0, usable_h, "fail-time-fallback", txn_id)
    )


def _build_history(
    cfg: WorldConfig,
    txn_id: str,
    cause: Cause,
    segment: Segment,
    tenure_months: int,
    failed_at: datetime,
) -> CustomerHistoryRecord:
    """Cause-conditional customer history. This is where the diagnostic signal lives."""
    gen = rng.stream(cfg.seed, "history", txn_id)

    successes = max(0, min(tenure_months, int(gen.normal(tenure_months * 0.86, 1.6))))
    failures = int(gen.poisson(0.8))
    days_late = float(max(0.0, gen.normal(1.2, 1.1)))
    contacts_sent = int(gen.poisson(0.35))
    responses = int(gen.binomial(contacts_sent, 0.45)) if contacts_sent else 0
    last_success = failed_at - timedelta(days=float(gen.uniform(25.0, 40.0)))

    match cause:
        case Cause.INSUFFICIENT_FUNDS:
            # Pays, but late and unreliably. The single strongest cue in the world.
            days_late = float(max(0.0, gen.normal(6.4, 2.3)))
            failures = int(gen.poisson(2.6))
            successes = max(0, successes - failures // 2)

        case Cause.CARD_EXPIRED:
            # Clean record that stops dead: nothing about the customer changed.
            days_late = float(max(0.0, gen.normal(0.7, 0.6)))
            failures = int(gen.poisson(0.3))

        case Cause.MANDATE_DEAD:
            # The distinguishing mark against card_expired is that recovery already
            # stopped some time ago and no contact has ever been answered.
            last_success = failed_at - timedelta(days=float(gen.uniform(65.0, 150.0)))
            failures = int(gen.poisson(2.2))
            responses = 0

        case Cause.RISK_BLOCK:
            # Newer relationship, and this debit is unusual for them.
            failures = int(gen.poisson(1.1))
            days_late = float(max(0.0, gen.normal(1.5, 1.0)))

        case Cause.AFA_TIMEOUT:
            # Has been asked before and did not answer. Predicts they will not answer now,
            # which is exactly what the contact decision turns on.
            contacts_sent = int(gen.poisson(1.5)) + 1
            responses = int(gen.binomial(contacts_sent, 0.12))

        case Cause.BANK_OUTAGE | Cause.ROUTE_DEGRADED:
            # Infrastructure failed, not the customer. The clean record *is* the signal.
            days_late = float(max(0.0, gen.normal(0.5, 0.4)))
            failures = int(gen.poisson(0.2))

    return CustomerHistoryRecord(
        successful_debits_12m=min(successes, 12),
        failed_debits_12m=min(failures, 12),
        last_success_at=last_success if successes > 0 else None,
        avg_days_late=round(days_late, 2),
        prior_contact_responses=min(responses, contacts_sent),
        prior_contacts_sent=contacts_sent,
        segment_label=segment,
    )


def _card_expiry(
    cfg: WorldConfig, txn_id: str, cause: Cause, failed_at: datetime
) -> tuple[int, int]:
    """Stored card expiry, which is a real but deliberately imperfect signal.

    Only about half of expired-card failures show a visibly past expiry; the rest are
    reissues where the number changed and the stored date still looks fine. And most dead
    mandates carry a perfectly plausible future date. Without that overlap the expiry field
    would resolve GW_21 on sight, and the most economically interesting confusion in the
    world — a paid contact against an abandon — would disappear.
    """
    gen = rng.stream(cfg.seed, "card-expiry", txn_id)
    visibly_expired = cause is Cause.CARD_EXPIRED and rng.bernoulli(
        cfg.seed, _EXPIRED_VISIBLE_SHARE, "expiry-visible", txn_id
    )
    if cause is Cause.MANDATE_DEAD and not rng.bernoulli(
        cfg.seed, _DEAD_LOOKS_VALID_SHARE, "dead-looks-valid", txn_id
    ):
        visibly_expired = True

    if visibly_expired:
        when = failed_at - timedelta(days=float(gen.uniform(5.0, 400.0)))
    else:
        when = failed_at + timedelta(days=float(gen.uniform(45.0, 1100.0)))
    return when.month, when.year


def generate_transactions(cfg: WorldConfig) -> list[Transaction]:
    """Generate the failed-payment population for a config."""
    out: list[Transaction] = []
    for i in range(cfg.n_transactions):
        txn_id = f"{cfg.name.upper()}-TXN-{i:05d}"
        rail = _sample_rail(cfg, txn_id)
        cause = sample_cause(cfg, txn_id, rail)
        plan = _sample_plan(cfg, txn_id)
        segment = _sample_segment(cfg, txn_id)
        bank = sample_bank(cfg, txn_id)
        route = sample_route(cfg, txn_id, rail)
        code = sample_error_code(cfg, txn_id, cause)
        message = sample_error_message(cfg, txn_id, code)
        failed_at = _failure_time(cfg, txn_id)

        gen = rng.stream(cfg.seed, "txn", txn_id)
        tenure = int(max(1, gen.poisson(cfg.segments[segment].expected_remaining_cycles)))

        horizon = cfg.bounds.expiry_horizon_days_by_rail[rail]
        is_card = rail is Rail.CARD_MANDATE
        exp_month, exp_year = _card_expiry(cfg, txn_id, cause, failed_at)

        out.append(
            Transaction(
                transaction_id=txn_id,
                merchant_id="MERCH-0001",
                customer_id=f"CUST-{i:05d}",
                rail=rail,
                amount_inr=plan.amount_inr,
                plan_name=plan.name,
                plan_tenure_months=tenure,
                error_code=code,
                error_message=message,
                bank_id=bank.bank_id,
                card_last4=f"{int(gen.integers(1000, 9999))}" if is_card else None,
                card_network=(
                    rng.choice(cfg.seed, CARD_NETWORKS, [0.42, 0.33, 0.25], "net", txn_id)
                    if is_card
                    else None
                ),
                card_exp_month=exp_month if is_card else None,
                card_exp_year=exp_year if is_card else None,
                mandate_id=f"MND-{i:05d}",
                mandate_created_at=failed_at - timedelta(days=30 * tenure),
                mandate_debits_this_cycle=1,
                acquirer_route=route,
                customer_history=_build_history(
                    cfg, txn_id, cause, segment, tenure, failed_at
                ),
                first_failure_at=failed_at,
                expires_at=failed_at + timedelta(days=horizon),
                true_cause=cause,
                segment=segment,
            )
        )
    return out


# --- persistence -----------------------------------------------------------------------


def write_jsonl(
    path: str | Path, records: Sequence[Transaction], *, include_ground_truth: bool
) -> None:
    """Write a dataset.

    ``include_ground_truth=False`` is used for ``history.jsonl``, the estimator's training
    split. The estimator must learn recovery probabilities from *observed outcomes*, never
    from labelled causes — otherwise it is reading the answer key by a slower route.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8", newline="\n") as fh:
        for record in records:
            payload = (
                record.model_dump(mode="json")
                if include_ground_truth
                else record.public_fields()
            )
            fh.write(json.dumps(payload, sort_keys=True) + "\n")


def read_jsonl(path: str | Path) -> Iterator[dict[str, object]]:
    with Path(path).open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)
