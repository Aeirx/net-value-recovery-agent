"""Serialise the world configuration and report the properties the thesis depends on.

Run via ``make config``. Publishing the whole configuration is one of the project's five
honesty moves: a simulator you wrote is a world you can rig, and the defence is to hand
the reader every parameter rather than the ones that flatter the result.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from netvalue.world.config import CONFIG_A, WorldConfig

REPO_ROOT = Path(__file__).resolve().parent.parent


def describe(cfg: WorldConfig) -> str:
    lines: list[str] = []
    add = lines.append

    add(f"config          {cfg.name}")
    add(f"hash            {cfg.config_hash()}")
    add(f"seed            {cfg.seed}")
    add(f"transactions    {cfg.n_transactions}")
    add(f"window          {cfg.clock.start:%Y-%m-%d} to {cfg.clock.end:%Y-%m-%d} "
        f"({cfg.clock.timezone})")

    add("")
    add("ambiguity  (the error code must not give the cause away)")
    add(f"  H(cause)              {cfg.cause_entropy_bits():.3f} bits")
    add(f"  H(cause | code)       {cfg.conditional_entropy_bits():.3f} bits")
    add(f"  I(cause; code)        {cfg.mutual_information_bits():.3f} bits "
        f"(ceiling {cfg.ambiguity.max_mutual_information_bits})")

    marginal = cfg.code_marginal()
    posterior = cfg.posterior_cause_given_code()
    per_code = cfg.conditional_entropy_by_code_bits()
    add("")
    add(f"  {'code':<8}{'P(code)':>9}{'top cause':>22}{'mass':>8}{'runner-up':>22}"
        f"{'mass':>8}{'H bits':>9}")
    for code in sorted(marginal, key=lambda c: -marginal[c]):
        ranked = sorted(posterior[code].items(), key=lambda kv: kv[1], reverse=True)
        (c1, p1), (c2, p2) = ranked[0], ranked[1]
        add(f"  {code:<8}{marginal[code]:>9.4f}{c1:>22}{p1:>8.1%}{c2:>22}{p2:>8.1%}"
            f"{per_code[code]:>9.3f}")

    add("")
    add("economics  (the contact crossovers are the thesis, and nothing hardcodes them)")
    for k in range(1, 5):
        add(f"  contact {k} needs P(recover) > {cfg.required_recovery_prob_asymptotic(k):>7.2%}")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(REPO_ROOT / "data" / "config_a.json"))
    args = parser.parse_args()

    digest = CONFIG_A.write_json(args.out)
    print(describe(CONFIG_A))
    print()
    print(f"wrote {args.out}")
    print(f"config_hash {digest}")


if __name__ == "__main__":
    main()
