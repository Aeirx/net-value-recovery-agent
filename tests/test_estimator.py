"""The estimator: calibrated, honest about what it does not know, and sealed off from truth.

Three properties are asserted, in order of how much the result depends on them:

1. **Calibration on held-out data.** The value engine multiplies these probabilities by
   rupees. An overconfident estimator makes the agent spend money it should not, and no
   accuracy metric would show it.
2. **Graceful backoff.** An error code the log never contained must fall through to a
   coarser rate, with the uncertainty widening to say so — never a crash, never a
   confident number conjured from nothing. Config B's ``GW_99`` is the live case.
3. **The boundary, in data.** The estimator must work from observables alone. If it could
   be made to depend on a cause, the whole result would be circular.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from netvalue.agent.calibration import (
    base_rate,
    brier_score,
    expected_calibration_error,
    log_loss,
    reliability,
)
from netvalue.agent.estimator import (
    RecoveryEstimator,
    _read_jsonl,
    select_shrinkage,
    split_by_transaction,
)
from netvalue.agent.features import BACKOFF_LEVELS, Features, from_history_row
from netvalue.agent.observation import (
    CustomerHistory,
    Observation,
    ObservedErrorCode,
    ObservedRail,
    ObservedSegment,
)
from netvalue.agent.policy import ActionKind

DATA = Path(__file__).resolve().parent.parent / "data"


@pytest.fixture(scope="module")
def rows() -> list[dict]:
    path = DATA / "history.jsonl"
    if not path.exists():
        pytest.skip("history not generated")
    return _read_jsonl(path)


@pytest.fixture(scope="module")
def split(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    return split_by_transaction(rows)


@pytest.fixture(scope="module")
def fitted(split: tuple[list[dict], list[dict]]) -> RecoveryEstimator:
    train, valid = split
    kappa, _ = select_shrinkage(train, valid)
    return RecoveryEstimator(kappa).fit(train)


def _observation(**overrides: object) -> Observation:
    base: dict = dict(
        transaction_id="T-1", merchant_id="M", customer_id="C",
        rail=ObservedRail.CARD_MANDATE, amount_inr=149.0, plan_tenure_months=6,
        error_code=ObservedErrorCode.GW_05, error_message="declined", bank_id="BK_HDFC",
        card_last4="1234", card_network="VISA", card_exp_month=12, card_exp_year=2028,
        mandate_id="MND", mandate_created_at=datetime(2025, 9, 1),
        mandate_debits_this_cycle=1, attempt_number=1, prior_attempts=(),
        customer_history=CustomerHistory(
            successful_debits_12m=6, failed_debits_12m=1, last_success_at=datetime(2026, 2, 5),
            avg_days_late=1.0, prior_contact_responses=0, prior_contacts_sent=0,
            segment_label=ObservedSegment.ENGAGED,
        ),
        first_failure_at=datetime(2026, 3, 10, 9), expires_at=datetime(2026, 3, 31, 9),
        observed_at=datetime(2026, 3, 10, 9),
    )
    base.update(overrides)
    return Observation(**base)


# ------------------------------------------------------------------ calibration


def test_beats_the_global_rate_on_held_out_data(
    fitted: RecoveryEstimator, split: tuple[list[dict], list[dict]]
) -> None:
    train, valid = split
    labels = [bool(r["succeeded"]) for r in valid]
    preds = [fitted.predict(from_history_row(r)).p for r in valid]
    constant = [base_rate([bool(r["succeeded"]) for r in train])] * len(valid)
    assert brier_score(preds, labels) < brier_score(constant, labels)
    assert log_loss(preds, labels) < log_loss(constant, labels)


def test_is_calibrated_on_held_out_data(
    fitted: RecoveryEstimator, split: tuple[list[dict], list[dict]]
) -> None:
    """When it says 70%, it should be right about 70% of the time.

    The 5-point ceiling is the gate the fit script enforces; it is asserted here too so a
    regression fails a test rather than only a script somebody has to remember to run.
    """
    _, valid = split
    labels = [bool(r["succeeded"]) for r in valid]
    preds = [fitted.predict(from_history_row(r)).p for r in valid]
    assert expected_calibration_error(preds, labels) <= 0.05


def test_no_reliability_bin_is_badly_off(
    fitted: RecoveryEstimator, split: tuple[list[dict], list[dict]]
) -> None:
    """ECE can hide one terrible bin behind several good ones. Populated bins must each
    be within a tolerance that scales with their own sampling noise."""
    _, valid = split
    labels = [bool(r["succeeded"]) for r in valid]
    preds = [fitted.predict(from_history_row(r)).p for r in valid]
    for b in reliability(preds, labels):
        if b.count < 30:
            continue
        se = (max(b.mean_predicted * (1 - b.mean_predicted), 0.01) / b.count) ** 0.5
        assert abs(b.gap) < 0.06 + 2.5 * se, f"bin [{b.low:.1f},{b.high:.1f}) gap {b.gap:+.3f}"


def test_learned_the_salary_effect_from_data(fitted: RecoveryEstimator) -> None:
    """The size of the payday effect is not in any config the agent can read. If it shows
    up in the estimate, the estimator learned it from outcomes — which is the point."""
    payday = Features("retry_after", "card_mandate", "GW_05", "1", "0", "engaged",
                      "payday", "late", "unknown", False)
    trough = Features("retry_after", "card_mandate", "GW_05", "1", "0", "engaged",
                      "trough", "late", "unknown", False)
    assert fitted.predict(payday).p > fitted.predict(trough).p


def test_learned_that_retries_decay(fitted: RecoveryEstimator) -> None:
    first = Features("retry_after", "card_mandate", "GW_05", "1", "0", "engaged",
                     "near", "prompt", "unknown", False)
    third = Features("retry_after", "card_mandate", "GW_05", "3+", "0", "engaged",
                     "near", "prompt", "unknown", False)
    assert fitted.predict(first).p > fitted.predict(third).p


# --------------------------------------------------------------- backoff / unknowns


def test_unseen_error_code_backs_off_rather_than_crashing(fitted: RecoveryEstimator) -> None:
    """GW_99 never appears in config A, so no cell keyed on it exists. The estimate must
    fall through to the per-intervention rate and *say so* via a wider interval."""
    obs = _observation(error_code=ObservedErrorCode.GW_99)
    est = fitted.predict_for(obs, ActionKind.RETRY_AFTER)
    known = fitted.predict_for(_observation(), ActionKind.RETRY_AFTER)

    assert 0.0 < est.p < 1.0
    assert est.n_support > 0, "backoff should land on a populated coarser cell"
    assert est.backoff_depth >= BACKOFF_LEVELS.index(("intervention",))
    assert (est.high - est.low) >= (known.high - known.low) * 0.8, (
        "an unseen code should not produce a tighter interval than a seen one"
    )


def test_empty_cell_returns_parent_exactly() -> None:
    est = RecoveryEstimator(10.0).fit(
        [
            {
                "transaction_id": f"T{i}", "rail": "card_mandate", "error_code": "GW_05",
                "amount_inr": 99.0, "segment_label": "engaged", "bank_id": "B",
                "attempt_index": 1, "contact_index": 1, "hours_since_first_failure": 24.0,
                "days_to_salary": 3, "avg_days_late": 1.0, "prior_contact_responses": 0,
                "prior_contacts_sent": 0, "card_expiry_visibly_past": False,
                "intervention": "retry_after", "succeeded": i % 3 == 0,
            }
            for i in range(30)
        ]
    )
    seen = Features("retry_after", "card_mandate", "GW_05", "1", "1", "engaged",
                    "near", "prompt", "unknown", False)
    unseen_segment = Features("retry_after", "card_mandate", "GW_05", "1", "1", "dormant",
                              "near", "prompt", "unknown", False)
    # Only the finest level distinguishes segment; every coarser cell is shared. The
    # unseen cell must report that it backed off one rung onto the 30 shared rows, and its
    # posterior strength must be exactly κ — the prior inherited from the parent, and
    # nothing else, because there is nothing else.
    pa, pb = est.predict(seen), est.predict(unseen_segment)
    assert pa.backoff_depth == 0 and pa.n_support == 30
    assert pb.backoff_depth == 1 and pb.n_support == 30
    assert pb.effective_n == pytest.approx(10.0)
    assert pb.high - pb.low > pa.high - pa.low, "no data must mean more uncertainty"


def test_shrinkage_pulls_sparse_cells_toward_the_parent() -> None:
    rows = [
        {
            "transaction_id": f"T{i}", "rail": "card_mandate", "error_code": "GW_05",
            "amount_inr": 99.0, "segment_label": "engaged", "bank_id": "B",
            "attempt_index": 1, "contact_index": 1, "hours_since_first_failure": 24.0,
            "days_to_salary": 3, "avg_days_late": 1.0, "prior_contact_responses": 0,
            "prior_contacts_sent": 0, "card_expiry_visibly_past": False,
            "intervention": "retry_after", "succeeded": i < 20,
        }
        for i in range(100)
    ]
    # One lone success in a cell that otherwise matches the 20% parent.
    rows.append({**rows[0], "transaction_id": "LONE", "segment_label": "dormant", "succeeded": True})
    weak = RecoveryEstimator(2.0).fit(rows)
    strong = RecoveryEstimator(200.0).fit(rows)
    lone = Features("retry_after", "card_mandate", "GW_05", "1", "1", "dormant",
                    "near", "prompt", "unknown", False)
    assert weak.predict(lone).p > strong.predict(lone).p
    # With strong shrinkage the lone success barely moves the cell off its parent's ~20%.
    # This assertion is what caught the global level being shrunk toward a 0.5 root prior
    # instead of taking the flat prior directly: under that bug it read 0.25, not 0.21.
    assert abs(strong.predict(lone).p - 0.21) < 0.03


def test_abandon_is_zero_with_no_uncertainty(fitted: RecoveryEstimator) -> None:
    est = fitted.predict_for(_observation(), ActionKind.ABANDON)
    assert est.p == 0.0 and est.low == 0.0 and est.high == 0.0


def test_interval_brackets_the_point_estimate(fitted: RecoveryEstimator) -> None:
    est = fitted.predict_for(_observation(), ActionKind.REQUEST_CARD_UPDATE)
    assert 0.0 <= est.low <= est.p <= est.high <= 1.0
    assert est.std > 0.0


# --------------------------------------------------------------------- the boundary


def test_features_cannot_express_a_cause() -> None:
    """If a cause could sneak in as a feature, the estimator would be reading the answer
    key by another route. The feature schema has no slot for it."""
    from dataclasses import fields

    names = {f.name for f in fields(Features)}
    assert not (names & {"cause", "true_cause", "recovery_probability"})


def test_split_keeps_a_transaction_on_one_side(rows: list[dict]) -> None:
    train, valid = split_by_transaction(rows)
    t_ids = {r["transaction_id"] for r in train}
    v_ids = {r["transaction_id"] for r in valid}
    assert not (t_ids & v_ids)
    assert len(train) + len(valid) == len(rows)


def test_supported_interventions_cover_the_action_space(fitted: RecoveryEstimator) -> None:
    """The value engine can only price actions the log has evidence for."""
    supported = fitted.supported_interventions()
    for needed in ("retry_now", "retry_after", "request_card_update", "escalate_to_human",
                   "switch_route_and_retry"):
        assert needed in supported, f"no evidence for {needed}; the agent cannot price it"
