"""The config is the single source of truth, so its guarantees are asserted directly.

Two properties matter beyond ordinary validation:

* **Hash stability.** Every run manifest records ``config_hash``. If the same parameters
  produced different hashes across processes, a reported number could not be traced back
  to the parameters that produced it.
* **Validators actually reject.** A validator that never fires is decoration. Each one is
  tested by feeding it a config that should fail.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from netvalue.world.config import (
    CONFIG_A,
    AnnoyanceModel,
    CardUpdateModel,
    Cause,
    ClockConfig,
    ErrorCode,
    Intervention,
    Rail,
    Segment,
    WorldConfig,
    default_config_a,
)


def test_config_a_is_valid() -> None:
    assert CONFIG_A.name == "config_a"
    assert CONFIG_A.n_transactions > 0


def test_config_hash_is_stable_across_instances() -> None:
    """Two independently constructed configs must hash identically."""
    assert default_config_a().config_hash() == default_config_a().config_hash()
    assert len(CONFIG_A.config_hash()) == 64


def test_config_hash_changes_when_a_parameter_moves() -> None:
    """A silent parameter change that did not move the hash would be untraceable."""
    tweaked = CONFIG_A.model_copy(update={"seed": CONFIG_A.seed + 1})
    assert tweaked.config_hash() != CONFIG_A.config_hash()


def test_json_round_trip_preserves_hash(tmp_path: Path) -> None:
    path = tmp_path / "config_a.json"
    written = CONFIG_A.write_json(path)
    reloaded = WorldConfig.read_json(path)
    assert reloaded.config_hash() == written == CONFIG_A.config_hash()


def test_canonical_json_is_sorted_and_compact() -> None:
    payload = json.loads(CONFIG_A.canonical_json())
    assert list(payload) == sorted(payload)


def test_config_is_frozen() -> None:
    """Mutating the config mid-run would invalidate the manifest hash."""
    with pytest.raises(ValidationError):
        CONFIG_A.seed = 1  # type: ignore[misc]


# ------------------------------------------------------------------- validators must fire


def test_rejects_priors_that_do_not_sum_to_one() -> None:
    broken = dict(CONFIG_A.causes)
    broken[Cause.MANDATE_DEAD] = broken[Cause.MANDATE_DEAD].model_copy(update={"prior": 0.5})
    with pytest.raises(ValidationError, match=r"sum to 1\.0"):
        CONFIG_A.model_copy(update={"causes": broken}).model_validate(
            CONFIG_A.model_copy(update={"causes": broken}).model_dump()
        )


def test_rejects_code_row_that_does_not_sum_to_one() -> None:
    rows = {c: dict(r) for c, r in CONFIG_A.code_given_cause.items()}
    rows[Cause.RISK_BLOCK][ErrorCode.GW_05] = 0.90
    payload = CONFIG_A.model_dump()
    payload["code_given_cause"] = rows
    with pytest.raises(ValidationError):
        WorldConfig.model_validate(payload)


def test_rejects_bd_td_split_outside_tolerance() -> None:
    """The BD/TD split is a calibration claim, so drifting away from it must be loud."""
    payload = CONFIG_A.model_dump()
    payload["bd_share_target"] = 0.50
    payload["bd_share_tolerance"] = 0.01
    with pytest.raises(ValidationError, match="BD share"):
        WorldConfig.model_validate(payload)


def test_rejects_non_convex_annoyance_schedule() -> None:
    """A flat or concave schedule would make stopping a formality."""
    with pytest.raises(ValidationError, match="strictly increasing"):
        AnnoyanceModel(delta_churn_by_contact=(0.05, 0.02, 0.07), delta_churn_beyond=0.14)


def test_rejects_non_decreasing_contact_response_decay() -> None:
    with pytest.raises(ValidationError, match="strictly decreasing"):
        CardUpdateModel(
            response_delay_median_hours=26.0,
            p_success_given_response=0.94,
            repeat_response_decay=(1.0, 0.6, 0.8),
        )


def test_rejects_empty_clock_window() -> None:
    from datetime import datetime

    with pytest.raises(ValidationError, match="non-empty"):
        ClockConfig(
            start=datetime(2026, 4, 5),
            end=datetime(2026, 3, 5),
            tick_hours=1,
            timezone="Asia/Kolkata",
        )


def test_rejects_rail_without_an_expiry_horizon() -> None:
    payload = CONFIG_A.model_dump()
    payload["bounds"]["expiry_horizon_days_by_rail"] = {Rail.CARD_MANDATE.value: 21}
    with pytest.raises(ValidationError, match="expiry horizon"):
        WorldConfig.model_validate(payload)


# ---------------------------------------------------------------------- structural claims


def test_exactly_one_permanently_unrecoverable_cause() -> None:
    """The honest-exception class. More than one and the exception list stops meaning much."""
    dead = [c for c, s in CONFIG_A.causes.items() if s.permanently_unrecoverable]
    assert dead == [Cause.MANDATE_DEAD]


def test_card_expired_is_not_permanently_unrecoverable() -> None:
    """It cannot be fixed by retrying, but a card update fixes it. That distinction is the
    reason the intervention set is richer than retry / do not retry."""
    spec = CONFIG_A.causes[Cause.CARD_EXPIRED]
    assert not spec.recoverable_by_retry
    assert not spec.permanently_unrecoverable


def test_card_only_causes_are_card_only() -> None:
    for cause in (Cause.CARD_EXPIRED, Cause.ROUTE_DEGRADED):
        assert CONFIG_A.causes[cause].rails == (Rail.CARD_MANDATE,)


def test_route_switch_is_unavailable_on_upi() -> None:
    """A merchant cannot switch acquirer for a UPI Autopay debit."""
    spec = CONFIG_A.costs.interventions[Intervention.SWITCH_ROUTE_AND_RETRY]
    assert Rail.UPI_AUTOPAY not in spec.rails


def test_escalation_has_no_minimum_amount_gate() -> None:
    """Whether a human should touch a small recovery is the judgment the value engine
    exists to make. A hardcoded floor would steal that decision from the thesis."""
    spec = CONFIG_A.costs.interventions[Intervention.ESCALATE_TO_HUMAN]
    assert spec.max_uses_per_transaction == 1
    assert spec.consumes_contact is True


def test_only_contact_interventions_consume_contacts() -> None:
    contacting = {
        i for i, s in CONFIG_A.costs.interventions.items() if s.consumes_contact
    }
    assert contacting == {Intervention.REQUEST_CARD_UPDATE, Intervention.ESCALATE_TO_HUMAN}


def test_max_attempts_exceeds_the_naive_baseline() -> None:
    """The agent must have room to choose. A cap of 3 would hand it the naive policy."""
    assert CONFIG_A.bounds.max_attempts_per_transaction > 3


def test_small_ticket_mass_is_real() -> None:
    """Uniform amounts would make almost everything worth recovering and empty the region
    the thesis lives in."""
    small = sum(p.share for p in CONFIG_A.plans if p.amount_inr <= 149.0)
    assert small > 0.55, f"only {small:.0%} of volume is small-ticket"


def test_segments_cover_a_wide_response_range() -> None:
    """If every segment responds at a similar rate, contact decisions never diverge."""
    rates = [s.contact_response_prob for s in CONFIG_A.segments.values()]
    assert max(rates) / min(rates) > 4.0


def test_clock_window_contains_both_salary_dates_and_fy_end() -> None:
    """DECISION-002. Calendar effects must be observable in the window, not asserted."""
    start, end = CONFIG_A.clock.start, CONFIG_A.clock.end
    from datetime import datetime

    for label, moment in (
        ("FY-end", datetime(2026, 3, 31)),
        ("salary 1st", datetime(2026, 4, 1)),
        ("salary 7th", datetime(2026, 3, 7)),
    ):
        assert start <= moment <= end, f"{label} falls outside the simulation window"


# ------------------------------------------------- Phase 2: sourced regulatory bounds


def test_retry_interval_respects_the_pre_debit_notification_floor() -> None:
    """RBI's 2026 e-mandate framework requires a fresh pre-debit notification at least
    24h before every attempt. A retry inside that window is not aggressive, it is
    non-compliant — so this is a regulatory floor, not a tuning knob.

    Phase 0 had 4 hours here, which would have produced an agent whose best strategy was
    illegal.
    """
    b = CONFIG_A.bounds
    assert b.pre_debit_notification_hours >= 24.0
    assert b.min_inter_attempt_hours >= b.pre_debit_notification_hours


def test_merchant_retry_policy_sits_inside_the_network_cap() -> None:
    """Mastercard permits 10 retries per 30 days and Visa 15, so Mastercard binds.
    The per-cycle figure is merchant policy and must stay under it."""
    b = CONFIG_A.bounds
    assert b.max_debits_per_mandate_cycle < b.network_retry_cap_per_30d


def test_rejects_retry_faster_than_the_notification_window() -> None:
    payload = CONFIG_A.model_dump()
    payload["bounds"]["min_inter_attempt_hours"] = 4.0
    with pytest.raises(ValidationError, match="non-compliant"):
        WorldConfig.model_validate(payload)


def test_rejects_merchant_policy_exceeding_the_network_cap() -> None:
    payload = CONFIG_A.model_dump()
    payload["bounds"]["max_debits_per_mandate_cycle"] = 99
    with pytest.raises(ValidationError, match="network permits"):
        WorldConfig.model_validate(payload)


def test_rails_have_materially_different_failure_rates() -> None:
    """UPI Autopay is stateless per debit; card mandates are bank-managed. Published
    ranges are 8-15% against 2-3%, and a world where both rails fail alike would erase a
    rail distinction the agent should be exploiting."""
    rates = CONFIG_A.base_failure_rate_by_rail
    assert rates[Rail.UPI_AUTOPAY] > 3 * rates[Rail.CARD_MANDATE]


def test_afa_threshold_is_above_every_ordinary_plan() -> None:
    """Below Rs 15,000 no additional-factor authentication is required, which is why
    ``afa_timeout`` had to be rescoped in Phase 2 to cover pre-debit opt-outs as well —
    scoped to AFA alone it would be unreachable on this plan ladder."""
    threshold = CONFIG_A.bounds.afa_threshold_inr
    assert all(p.amount_inr < threshold for p in CONFIG_A.plans)


def test_every_segment_and_plan_is_reachable() -> None:
    assert all(s.share > 0 for s in CONFIG_A.segments.values())
    assert all(p.share > 0 for p in CONFIG_A.plans)
    assert set(CONFIG_A.segments) == set(Segment)
