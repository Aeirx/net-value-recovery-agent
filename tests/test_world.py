"""Assertions about the world the generator actually produces.

``tests/test_ambiguity.py`` checks the *configured* distribution. This module checks the
*realised* one, and the distinction is not academic: Phase 3 found that the config-level
ambiguity calculation used raw cause priors, while the generator renormalises them per
rail. Measured correctly, ``GW_33`` was carrying 70.4% of its mass on a single cause
against a 70% ceiling — a breach of the project's central premise that the config-level
test reported as a comfortable pass.

A guarantee checked against a quantity the world does not have is not a guarantee.
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from pathlib import Path

import pytest

from netvalue.world.banks import BANKS, build_world_health
from netvalue.world.config import (
    CONFIG_A,
    CONFIG_B,
    Cause,
    DeclineClass,
    ErrorCode,
    Rail,
    WorldConfig,
)
from netvalue.world.generator import generate_transactions

DATA = Path(__file__).resolve().parent.parent / "data"

#: Large enough that per-code cells have tight error bars, so the test measures the
#: generator rather than one particular draw.
_PROBE_N = 6000


def _entropy(counts: Counter[str]) -> float:
    n = sum(counts.values())
    return -math.fsum((c / n) * math.log2(c / n) for c in counts.values() if c)


@pytest.fixture(scope="module")
def probe() -> list:
    return generate_transactions(CONFIG_A.model_copy(update={"n_transactions": _PROBE_N}))


@pytest.fixture(scope="module")
def dataset_a() -> list[dict]:
    path = DATA / "dataset_a.jsonl"
    if not path.exists():
        pytest.skip("datasets not generated; run scripts/generate_datasets.py")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


@pytest.fixture(scope="module")
def dataset_b() -> list[dict]:
    path = DATA / "dataset_b.jsonl"
    if not path.exists():
        pytest.skip("datasets not generated; run scripts/generate_datasets.py")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# ------------------------------------------------------------- realised ambiguity


def test_generated_world_matches_the_effective_prior(probe: list) -> None:
    """The generator must realise ``effective_cause_prior``, not the raw priors."""
    observed = Counter(t.true_cause for t in probe)
    expected = CONFIG_A.effective_cause_prior()
    for cause, want in expected.items():
        got = observed[cause] / len(probe)
        se = math.sqrt(max(want * (1 - want), 1e-9) / len(probe))
        assert abs(got - want) < 4 * se, (
            f"{cause}: generator produced {got:.4f}, effective prior says {want:.4f}"
        )


def test_realised_ambiguity_respects_the_ceiling(probe: list) -> None:
    """No error code may concentrate, measured on generated data rather than on config."""
    joint: defaultdict[str, Counter[str]] = defaultdict(Counter)
    for t in probe:
        joint[t.error_code][t.true_cause] += 1

    ceiling = CONFIG_A.ambiguity.max_posterior_mass_per_code
    offenders = {}
    for code, counts in joint.items():
        n = sum(counts.values())
        cause, top = counts.most_common(1)[0]
        share = top / n
        # Allow two standard errors, so the test fails on a real breach rather than on an
        # unlucky draw.
        if share - 2 * math.sqrt(share * (1 - share) / n) > ceiling:
            offenders[code] = (cause, share)
    assert not offenders, f"codes concentrate above {ceiling:.0%}: {offenders}"


def test_realised_conditional_entropy_floor(probe: list) -> None:
    joint: defaultdict[str, Counter[str]] = defaultdict(Counter)
    for t in probe:
        joint[t.error_code][t.true_cause] += 1
    floor = CONFIG_A.ambiguity.min_conditional_entropy_bits
    low = {c: _entropy(k) for c, k in joint.items() if _entropy(k) < floor}
    assert not low, f"H(cause | code) below {floor} bits for {low}"


def test_realised_mutual_information_ceiling(probe: list) -> None:
    joint: defaultdict[str, Counter[str]] = defaultdict(Counter)
    causes: Counter[str] = Counter()
    for t in probe:
        joint[t.error_code][t.true_cause] += 1
        causes[t.true_cause] += 1
    n = len(probe)
    h_cond = math.fsum(
        (sum(k.values()) / n) * _entropy(k) for k in joint.values()
    )
    mi = _entropy(causes) - h_cond
    assert 0.0 < mi <= CONFIG_A.ambiguity.max_mutual_information_bits, f"I = {mi:.3f}"


def test_realised_bd_share(probe: list) -> None:
    bd = sum(
        1 for t in probe if CONFIG_A.causes[t.true_cause].decline_class is DeclineClass.BD
    ) / len(probe)
    assert abs(bd - CONFIG_A.bd_share_target) < CONFIG_A.bd_share_tolerance + 0.01


# ------------------------------------------------------------- structural invariants


def test_card_only_causes_never_appear_on_upi(probe: list) -> None:
    """A UPI Autopay mandate has no card to expire and no acquirer to switch."""
    for t in probe:
        if t.rail is Rail.UPI_AUTOPAY:
            assert t.true_cause not in {Cause.CARD_EXPIRED, Cause.ROUTE_DEGRADED}
            assert t.acquirer_route is None
            assert t.card_last4 is None


def test_card_expiry_is_an_imperfect_signal(probe: list) -> None:
    """If a visibly-past expiry resolved GW_21 outright, the most economically loaded
    ambiguity in the world would collapse into a lookup."""
    expired = [t for t in probe if t.true_cause is Cause.CARD_EXPIRED]
    visible = [
        t for t in expired
        if t.card_exp_year is not None
        and (t.card_exp_year, t.card_exp_month) < (t.first_failure_at.year, t.first_failure_at.month)
    ]
    share = len(visible) / len(expired)
    assert 0.35 < share < 0.75, (
        f"{share:.0%} of expired cards are visibly expired; the field must stay partial"
    )


def test_every_transaction_expires_after_it_fails(probe: list) -> None:
    for t in probe:
        assert t.expires_at > t.first_failure_at
        horizon_days = (t.expires_at - t.first_failure_at).days
        assert horizon_days == CONFIG_A.bounds.expiry_horizon_days_by_rail[t.rail]


def test_outages_exist_and_are_bounded(probe: list) -> None:
    health = build_world_health(CONFIG_A)
    windows = health.banks.all_windows()
    assert windows, "a world with no outages cannot exercise bank_outage at all"
    assert all(w.hours > 0 for w in windows)
    covered = {w.target for w in windows}
    assert len(covered) >= len(BANKS) // 2


# ------------------------------------------------------------- the held-out regime


def test_config_b_is_structurally_different(dataset_b: list[dict]) -> None:
    """Config B must differ in kind, not merely in difficulty."""
    assert not CONFIG_B.regime.is_baseline()
    codes = Counter(r["error_code"] for r in dataset_b)
    assert codes[ErrorCode.GW_99.value] > 0, "the unseen code never appeared"


def test_unseen_code_is_absent_from_the_tuning_regime(dataset_a: list[dict]) -> None:
    """If GW_99 leaked into config A it would not be unseen, and the held-out run would
    measure nothing."""
    codes = {r["error_code"] for r in dataset_a}
    assert ErrorCode.GW_99.value not in codes


def test_correlated_outage_exists_only_in_config_b() -> None:
    a = [w for w in build_world_health(CONFIG_A).banks.all_windows() if w.correlated]
    b = [w for w in build_world_health(CONFIG_B).banks.all_windows() if w.correlated]
    assert not a
    assert len({w.target for w in b}) == CONFIG_B.regime.correlated_outage_banks
    assert len({w.start for w in b}) == 1, "a correlated outage must be simultaneous"


# ------------------------------------------------------------- the boundary, in data


def test_history_contains_no_ground_truth() -> None:
    """The estimator's training split must not leak causes.

    ``tests/test_boundary.py`` stops the agent *importing* ground truth. This stops it
    being *handed* ground truth, which would defeat the boundary just as completely and
    far more quietly.
    """
    path = DATA / "history.jsonl"
    if not path.exists():
        pytest.skip("history not generated")
    banned = {"true_cause", "cause", "recovery_probability", "segment"}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        keys = set(json.loads(line))
        leaked = keys & banned
        assert not leaked, f"history.jsonl leaks ground truth: {leaked}"


def test_history_covers_the_intervention_space() -> None:
    """An estimator can only reason about actions it has evidence for.

    A logging policy that only ever retried would leave the agent blind about contacts and
    escalations — and blind in a direction that happens to favour the thesis, which is
    exactly the kind of convenient gap a reviewer should be able to rule out.
    """
    path = DATA / "history.jsonl"
    if not path.exists():
        pytest.skip("history not generated")
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    by_intervention = Counter(r["intervention"] for r in rows)
    assert len(by_intervention) >= 5, f"only {len(by_intervention)} interventions logged"
    assert min(by_intervention.values()) >= 100, f"thin coverage: {by_intervention}"

    successes = Counter(r["intervention"] for r in rows if r["succeeded"])
    assert all(successes[i] > 0 for i in by_intervention), (
        f"an intervention never succeeded in the log, so its rate cannot be estimated: "
        f"{ {i: successes[i] for i in by_intervention} }"
    )


def test_history_population_is_disjoint_from_the_evaluation_set(
    dataset_a: list[dict],
) -> None:
    """Training on the transactions you score on would inflate the estimator for free."""
    path = DATA / "history.jsonl"
    if not path.exists():
        pytest.skip("history not generated")
    hist_ids = {
        json.loads(line)["transaction_id"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    }
    eval_ids = {r["transaction_id"] for r in dataset_a}
    assert not (hist_ids & eval_ids), "history and evaluation populations overlap"


# ------------------------------------------------------------- the frozen artefacts


def test_manifest_matches_the_files_on_disk() -> None:
    """The freeze is only meaningful if it is verifiable."""
    import hashlib

    manifest_path = DATA / "manifest.json"
    if not manifest_path.exists():
        pytest.skip("datasets not frozen")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    for filename, expected in manifest["files"].items():
        actual = hashlib.sha256((DATA / filename).read_bytes()).hexdigest()
        assert actual == expected, (
            f"{filename} has changed since the freeze. If this was deliberate, re-freeze "
            f"with --force and record why in DECISIONS.md; if not, every number computed "
            f"from it is now untraceable."
        )


def test_manifest_config_hashes_match_the_code() -> None:
    manifest_path = DATA / "manifest.json"
    if not manifest_path.exists():
        pytest.skip("datasets not frozen")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["configs"]["config_a"]["hash"] == CONFIG_A.config_hash()
    assert manifest["configs"]["config_b"]["hash"] == CONFIG_B.config_hash()


@pytest.mark.parametrize("cfg", [CONFIG_A, CONFIG_B], ids=["a", "b"])
def test_generation_is_deterministic(cfg: WorldConfig) -> None:
    """Same seed, same world. Without this the freeze guarantees nothing."""
    small = cfg.model_copy(update={"n_transactions": 120})
    first = generate_transactions(small)
    second = generate_transactions(small)
    assert [t.model_dump(mode="json") for t in first] == [
        t.model_dump(mode="json") for t in second
    ]
