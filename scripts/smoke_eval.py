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

from netvalue.eval.metrics import summarise
from netvalue.eval.runner import EpisodeRunner
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=50, help="transactions once Phase 3 lands")
    args = parser.parse_args()

    print(f"smoke evaluation  (n={args.n})")
    print(f"config            {CONFIG_A.name} @ {CONFIG_A.config_hash()[:12]}")

    failures = check_config(CONFIG_A)
    failures += check_world(CONFIG_A, args.n)
    failures += check_harness(CONFIG_A, args.n)

    # Phase 5+ stages append here: estimator -> diagnosis -> agent -> report.
    print("stages            config [ok]  world [ok]  harness [ok]  agent [phase 7]")

    if failures:
        print("\nFAILED")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("\nPASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
