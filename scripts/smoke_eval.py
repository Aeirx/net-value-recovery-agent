"""End-to-end smoke evaluation, run on every push.

Its job is to fail loudly the moment the pipeline stops working end to end — not to
produce a headline number. Each phase wires its stage in here as it lands, so CI keeps
exercising the whole chain rather than the newest piece in isolation.

Phase 1 exercises what exists: the config loads, validates, hashes stably, and the
properties the thesis depends on still hold.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from netvalue.agent.calibration import base_rate, brier_score, expected_calibration_error
from netvalue.agent.diagnose.rules import RulesDiagnoser
from netvalue.agent.estimator import RecoveryEstimator, _read_jsonl, split_by_transaction
from netvalue.agent.features import from_history_row
from netvalue.eval.diagnosis import evaluate as evaluate_diagnosis
from netvalue.eval.metrics import summarise
from netvalue.eval.runner import EpisodeRunner, to_observation
from netvalue.policies.max_recovery import (
    MaxRecoveryOraclePolicy,
    MaxRecoveryPolicy,
    build_truth,
)
from netvalue.policies.naive import NaiveRetryPolicy
from netvalue.policies.no_retry import NoRetryPolicy
from netvalue.world.banks import build_world_health
from netvalue.world.config import CONFIG_A, Cause, Rail, WorldConfig
from netvalue.world.generator import generate_transactions


def check_config(cfg: WorldConfig) -> list[str]:
    failures: list[str] = []

    mi = cfg.mutual_information_bits()
    if mi > cfg.ambiguity.max_mutual_information_bits:
        failures.append(f"I(cause; code) = {mi:.3f} bits exceeds the ceiling")

    posterior = cfg.posterior_cause_given_code()
    for code, dist in posterior.items():
        top = max(dist.values())
        if top > cfg.ambiguity.max_posterior_mass_per_code:
            failures.append(f"{code} concentrates at {top:.1%}")

    if cfg.required_recovery_prob_asymptotic(3) < 0.10:
        failures.append("contact 3 is too cheap; the annoyance cost has stopped biting")

    if cfg.config_hash() != WorldConfig.model_validate(cfg.model_dump()).config_hash():
        failures.append("config hash is not stable across a round trip")

    return failures


def check_world(cfg: WorldConfig, n: int) -> list[str]:
    """Generate a small population and confirm the world still behaves.

    Deliberately checks *generated data* rather than configured parameters. Phase 3 found
    a breach that only appeared in generated data: the config-level ambiguity calculation
    used raw cause priors while the generator renormalises them per rail, so a real
    violation of the 70% ceiling was reported as a comfortable pass.
    """
    failures: list[str] = []
    probe = cfg.model_copy(update={"n_transactions": n})
    txns = generate_transactions(probe)

    if len(txns) != n:
        failures.append(f"generator produced {len(txns)} of {n} transactions")

    leaked = [
        t for t in txns
        if t.rail is Rail.UPI_AUTOPAY
        and t.true_cause in {Cause.CARD_EXPIRED, Cause.ROUTE_DEGRADED}
    ]
    if leaked:
        failures.append(f"{len(leaked)} card-only causes appeared on UPI Autopay")

    if any(t.expires_at <= t.first_failure_at for t in txns):
        failures.append("a transaction expires at or before it fails")

    if not build_world_health(cfg).banks.all_windows():
        failures.append("no bank outages exist, so bank_outage is unexercised")

    manifest = Path(__file__).resolve().parent.parent / "data" / "manifest.json"
    print(f"datasets          {'frozen' if manifest.exists() else 'NOT FROZEN'}")
    return failures


def check_harness(cfg: WorldConfig, n: int) -> list[str]:
    """Run every baseline over a small population and check the scoreboard still holds.

    Cheap enough for every push, and it catches the class of failure that matters most
    here: a ceiling that quietly stops spending. When the max-recovery baseline made no
    customer contacts it incurred no annoyance cost, which would have left the thesis
    with nothing to trade against — and every number would still have looked plausible.
    """
    failures: list[str] = []
    probe = cfg.model_copy(update={"n_transactions": n})
    txns = generate_transactions(probe)
    truth = build_truth(txns)
    runner = EpisodeRunner(cfg, build_world_health(cfg), replication=0)

    scores: dict[str, float] = {}
    for policy in (
        NoRetryPolicy(), NaiveRetryPolicy(),
        MaxRecoveryPolicy(cfg, truth), MaxRecoveryOraclePolicy(cfg, truth),
    ):
        m = summarise(policy.name, runner.run(policy, txns))
        scores[policy.name] = m.net_value_inr
        if m.policy == "max_recovery":
            if m.total_contacts == 0:
                failures.append("the max-recovery ceiling made no contacts")
            if m.annoyance_cost_inr <= 0.0:
                failures.append("the max-recovery ceiling incurred no annoyance cost")

    if scores["no_retry"] != 0.0:
        failures.append("the no-retry floor is not exactly zero")
    if scores["max_recovery"] <= scores["no_retry"]:
        failures.append("the ceiling does not beat the floor")

    ceiling = scores["max_recovery"]
    print(f"baselines         floor 0  naive {scores['naive_retry']:,.0f}  "
          f"ceiling {ceiling:,.0f}  oracle {scores['max_recovery_oracle']:,.0f}")
    return failures


def check_estimator() -> list[str]:
    """Fit on the frozen log and confirm the estimator is still fit to price with.

    The value engine multiplies these probabilities by rupees, so an uncalibrated
    estimator makes the agent spend money it should not while every downstream number
    still looks plausible. Cheap enough to run on every push.
    """
    path = Path(__file__).resolve().parent.parent / "data" / "history.jsonl"
    if not path.exists():
        print("estimator         skipped (no history)")
        return []
    train, valid = split_by_transaction(_read_jsonl(path))
    est = RecoveryEstimator(20.0).fit(train)
    labels = [bool(r["succeeded"]) for r in valid]
    preds = [est.predict(from_history_row(r)).p for r in valid]
    constant = [base_rate([bool(r["succeeded"]) for r in train])] * len(valid)
    brier, brier0 = brier_score(preds, labels), brier_score(constant, labels)
    ece = expected_calibration_error(preds, labels)
    print(f"estimator         brier {brier:.4f} (global {brier0:.4f})  ece {ece:.4f}")
    failures: list[str] = []
    if brier >= brier0:
        failures.append("estimator does not beat the global rate")
    if ece > 0.05:
        failures.append(f"estimator ECE {ece:.4f} exceeds 0.05")
    return failures


def check_diagnosis(cfg: WorldConfig, n: int) -> list[str]:
    """The rules arm is free and deterministic, so it runs on every push.

    It is also the ablation floor: if it silently degraded toward guessing, beating it
    would stop meaning anything and the "AI judgment" claim would quietly hollow out.
    """
    probe = cfg.model_copy(update={"n_transactions": n})
    txns = generate_transactions(probe)
    observations = [
        to_observation(t, now=t.first_failure_at, attempt_number=1, contacts_used=0, prior=())
        for t in txns
    ]
    diagnoser = RulesDiagnoser()
    posteriors = [diagnoser.diagnose(o) for o in observations]
    report = evaluate_diagnosis(cfg, "rules", posteriors, [t.true_cause.value for t in txns])
    print(f"diagnosis         rules acc {report.accuracy:.1%}  "
          f"mean regret Rs {report.mean_regret_inr:,.1f}  ece {report.ece:.3f}")
    failures: list[str] = []
    if report.accuracy < 0.45:
        failures.append(f"rules arm degraded to {report.accuracy:.1%}; the floor is a strawman")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=50, help="transactions once Phase 3 lands")
    args = parser.parse_args()

    print(f"smoke evaluation  (n={args.n})")
    print(f"config            {CONFIG_A.name} @ {CONFIG_A.config_hash()[:12]}")

    failures = check_config(CONFIG_A)
    failures += check_world(CONFIG_A, args.n)
    failures += check_harness(CONFIG_A, args.n)
    failures += check_estimator()
    failures += check_diagnosis(CONFIG_A, args.n)

    # Phase 7+ stages append here: value engine -> agent -> report.
    print("stages            config [ok]  world [ok]  harness [ok]  estimator [ok]  "
          "diagnosis [ok]  agent [phase 7]")

    if failures:
        print("\nFAILED")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("\nPASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
