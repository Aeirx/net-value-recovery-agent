"""Run every baseline over the frozen dataset and report with confidence intervals.

This is the scoreboard, and it exists before the agent does. Measurement first is what
makes heavy agentic coding work: the agent in Phases 5-7 iterates against real numbers
rather than against impressions.

Two numbers matter when this finishes:

* the **net value to beat** — ``max_recovery``, the success-rate ceiling
* the **gross recovery to fall short of** — the same policy's recovered total

The submission's whole claim is to lose the second while winning the first.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# The Windows console defaults to cp1252, which cannot encode the rupee sign. Every
# figure in this project is in rupees, so the report would be unreadable without this.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

from netvalue.agent.diagnose.rules import RulesDiagnoser
from netvalue.eval.bootstrap import Interval, cluster_bootstrap
from netvalue.eval.metrics import PolicyMetrics, paired_deltas, summarise, win_rate
from netvalue.eval.report import (
    comparison_table,
    metrics_table,
    plot_net_vs_gross,
    thesis_line,
    write_html,
)
from netvalue.eval.runner import EpisodeResult, EpisodeRunner, to_observation
from netvalue.policies.max_recovery import (
    MaxRecoveryOraclePolicy,
    MaxRecoveryPolicy,
    build_truth,
)
from netvalue.policies.naive import NaiveRetryPolicy
from netvalue.policies.net_value import build as build_agent
from netvalue.policies.no_retry import NoRetryPolicy
from netvalue.world.banks import build_world_health
from netvalue.world.config import CONFIG_A, CONFIG_B, WorldConfig
from netvalue.world.generator import load_transactions

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA = REPO_ROOT / "data"
REPORTS = REPO_ROOT / "reports"

#: The comparison the thesis is stated against.
CEILING = "max_recovery"


def build_policies(cfg: WorldConfig, truth: dict, observations: list, depth: int) -> list:
    """Every strategy behind one protocol, so the harness cannot tell them apart.

    The agent is constructed fresh per replication like the rest: it carries per-transaction
    belief state, and reusing one across replications would leak what it learned in an
    earlier world into a later one.
    """
    return [
        NoRetryPolicy(),
        NaiveRetryPolicy(),
        MaxRecoveryPolicy(cfg, truth),
        MaxRecoveryOraclePolicy(cfg, truth),
        build_agent(RulesDiagnoser(), depth=depth, reference_observations=observations),
    ]


def run(
    cfg: WorldConfig, dataset: Path, replications: int, depth: int = 4
) -> tuple[dict[str, list[EpisodeResult]], dict[str, PolicyMetrics]]:
    txns = load_transactions(dataset)
    truth = build_truth(txns)
    observations = [
        to_observation(t, now=t.first_failure_at, attempt_number=1, contacts_used=0, prior=())
        for t in txns
    ]

    results: dict[str, list[EpisodeResult]] = {}
    for replication in range(replications):
        # One realised world per replication, shared by every policy at that index. This
        # is what makes the deltas paired rather than two independent samples.
        health = build_world_health(cfg, replication)
        runner = EpisodeRunner(cfg, health, replication)
        for policy in build_policies(cfg, truth, observations, depth):
            results.setdefault(policy.name, []).extend(runner.run(policy, txns))

    metrics = {name: summarise(name, rows) for name, rows in results.items()}
    return results, metrics


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", choices=["a", "b"], default="a")
    parser.add_argument(
        "--replications", type=int, default=30,
        help="seeded worlds; each is faced identically by every policy",
    )
    parser.add_argument("--resamples", type=int, default=10_000)
    parser.add_argument(
        "--depth", type=int, default=4,
        help="value-engine lookahead. Depth 1 is the greedy rule; 4 is the first depth "
             "that can see a contact after the debit budget is spent.",
    )
    args = parser.parse_args()

    cfg = CONFIG_A if args.config == "a" else CONFIG_B
    dataset = DATA / f"dataset_{args.config}.jsonl"
    if not dataset.exists():
        print(f"missing {dataset}; run scripts/generate_datasets.py first")
        return 1

    if args.config == "b":
        print("!! config B is the held-out regime. It is meant to be run ONCE, in Phase 8,")
        print("!! and never tuned against. If this is not that run, stop.\n")

    results, metrics = run(cfg, dataset, args.replications, args.depth)
    order = [
        "no_retry", "naive_retry", "max_recovery", "net_value", "max_recovery_oracle",
    ]
    rows = [metrics[name] for name in order if name in metrics]

    print(f"# Baselines — config {args.config.upper()}")
    print(f"\nconfig hash `{cfg.config_hash()[:16]}` · {args.replications} replications · "
          f"{rows[0].n_transactions} transactions\n")
    print(metrics_table(rows))
    print()

    comparisons: list[tuple[str, Interval, float]] = []
    for name in order:
        if name == CEILING or name not in results:
            continue
        deltas = paired_deltas(results[name], results[CEILING])
        interval = cluster_bootstrap(deltas, n_resamples=args.resamples)
        comparisons.append((name, interval, win_rate(deltas)))
    print(comparison_table(CEILING, comparisons))

    ceiling = metrics[CEILING]
    print(f"\n**Net value to beat:** ₹{ceiling.net_value_inr:,.0f}")
    print(f"**Gross recovery to fall short of:** ₹{ceiling.gross_recovered_inr:,.0f}")
    # Name the agent explicitly. Picking the highest-net policy selected the *oracle*,
    # which is the ablation's ceiling rather than a contender — so the headline sentence
    # was describing a policy that reads ground truth.
    agent = metrics.get("net_value")
    if agent is not None:
        print(f"\n_The thesis row:_ {thesis_line(agent, ceiling)}")

    REPORTS.mkdir(exist_ok=True)
    image = plot_net_vs_gross(rows, REPORTS / f"baselines_{args.config}.png")
    page = write_html(
        rows, comparisons, CEILING, REPORTS / f"baselines_{args.config}.html",
        config_hash=cfg.config_hash(), n_replications=args.replications, image=image,
    )
    (REPORTS / f"baselines_{args.config}.json").write_text(
        json.dumps(
            {
                "config_hash": cfg.config_hash(),
                "replications": args.replications,
                "policies": {
                    m.policy: {
                        "net_value_inr": round(m.net_value_inr, 2),
                        "gross_recovered_inr": round(m.gross_recovered_inr, 2),
                        "recovery_rate": round(m.recovery_rate, 4),
                        "attempt_cost_inr": round(m.attempt_cost_inr, 2),
                        "annoyance_cost_inr": round(m.annoyance_cost_inr, 2),
                        "attempts": m.total_attempts,
                        "contacts": m.total_contacts,
                        "abandoned_but_recoverable": m.abandoned_but_recoverable,
                        "gate_fires": m.gate_fires,
                        "terminal_reasons": m.terminal_reasons,
                    }
                    for m in rows
                },
                "deltas_vs_ceiling": {
                    name: {
                        "mean": round(iv.mean, 2),
                        "ci_low": round(iv.low, 2),
                        "ci_high": round(iv.high, 2),
                        "win_rate": round(wins, 4),
                        "sign_resolved": iv.excludes_zero,
                    }
                    for name, iv, wins in comparisons
                },
            },
            indent=2, sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"\nwrote {page}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
