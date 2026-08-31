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

from netvalue.world.config import CONFIG_A, WorldConfig


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=50, help="transactions once Phase 3 lands")
    args = parser.parse_args()

    print(f"smoke evaluation  (n={args.n} requested)")
    print(f"config            {CONFIG_A.name} @ {CONFIG_A.config_hash()[:12]}")

    failures = check_config(CONFIG_A)

    # Phase 3+ stages append here: generate -> run baselines -> run agent -> report.
    print("stages            config [ok]  world [phase 3]  baselines [phase 4]  "
          "agent [phase 7]")

    if failures:
        print("\nFAILED")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("\nPASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
