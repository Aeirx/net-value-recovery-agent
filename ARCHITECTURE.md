# Architecture

## The problem

Every failed recurring payment is an asset with a declining recovery probability, a
per-attempt cost, a customer-annoyance cost, and a hard expiry. At each step the system
asks two questions, and the second is the one production retry engines do not ask:

1. What is actually wrong, given ambiguous evidence?
2. Is recovering this worth what recovery will cost?

## The split that makes the result meaningful

```
                          THE BOUNDARY
   world/  (ground truth)      │      agent/  (observation only)
   ───────────────────────     │      ─────────────────────────
   hidden cause                │      error code (one-to-many)
   recovery physics            │      amount, rail, bank, card meta
   bank health, outages        │      attempt number, prior attempts
   will_respond_to_contact     │      customer history
                               │
        history.jsonl  ────────┼────►  estimator.py  ──►  P-hat + uncertainty
        (outcomes, no causes)  │
                               │
                          enforced by
                     tests/test_boundary.py
```

The agent **estimates** the world's physics from observed historical outcomes. It never
reads them. Without this split the value engine and the simulator would share a model of
`P(recover | cause, intervention)`, the agent would win by construction, and every number
in the submission would be a tautology.

The guard walks the import graph with the AST, so it catches imports inside functions,
inside `TYPE_CHECKING` blocks, and inside branches that never execute.

## Pipeline

```
world.generator ──► dataset_a.jsonl   (tuning regime)
                ──► dataset_b.jsonl   (held-out: correlated outage + unseen code)
                ──► history.jsonl     (estimator training split, no causes)
                          │
                          ▼
   eval.runner ──► global discrete-event clock ──► Policy.decide(observation)
                          │                              │
                          │                     ┌────────┴────────┐
                          │                     │                 │
                          │              agent.diagnose      agent.estimator
                          │              posterior over       P-hat with
                          │              7 causes             uncertainty
                          │                     │                 │
                          │                     └────────┬────────┘
                          │                              ▼
                          │                       agent.belief  (Bayesian update
                          │                              │        on failed attempts)
                          │                              ▼
                          │                       agent.value   (finite-horizon
                          │                              │        backward induction)
                          │                              ▼
                          │                       agent.bounds  (deterministic gates;
                          │                              │        can only shrink)
                          ▼                              ▼
                    eval.ledger  ◄──────────────────  Action + audit row
                          │
                          ▼
        eval.metrics ─► eval.bootstrap ─► eval.sweep / eval.ablation ─► eval.report
```

## Design decisions worth naming

**A global discrete-event clock, not per-transaction timelines.** World state — bank
health, calendar effects, route degradation — is a function of absolute time, shared by
generation and by every recovery attempt. Without shared time a "bank outage" is a
per-transaction attribute correlated with nothing, and the correlated multi-bank outage in
config B could not exist.

**Diagnosis returns a posterior, not a label.** Committing to the argmax throws away the
ambiguity that justifies having a model in the system at all. `I(cause; code) = 0.705
bits` against `H(cause) = 2.608` — the error code narrows the cause and never resolves it.

**Stopping is backward induction, not a one-step threshold.** A myopic "expected value of
continuing exceeds cost of continuing" rule has no term that can represent option value,
and abandons transactions it should hold — waiting out a bank outage looks like pure cost
with no immediate payoff. Solving `(belief, attempts_used, contacts_used, hours_remaining)`
backward from the expiry horizon produces option value from the recursion rather than
asserting it.

**Economics and bounds are deterministic; only ambiguous inference is model-driven.**
Bounds you cannot verify are not bounds, and a model may not be the thing that enforces a
cap. The gate layer can only *shrink* the admissible action set, and logs which gate fired.

**The audit log is the product.** One row per action: observation, posterior, estimator
`P̂` and interval, Q-values for every action considered, gate fired, action taken, cost,
outcome, timestamp. It covers three of the four scoring criteria in one file.

## Evaluation design

Four baselines behind the same `Policy` protocol, so the harness cannot accidentally give
the agent a longer horizon or a cheaper attempt:

| # | Strategy | Represents |
|---|---|---|
| 1 | No retry | Floor |
| 2 | Naive fixed retry, 3 attempts 24h apart | What most production systems do |
| 3 | Max-recovery, ignoring cost | **Razorpay's own optimisation target** |
| 3b | Max-recovery with oracle diagnosis | The true success-rate ceiling |
| 4 | Net-value agent | This project |

Thirty seeded replications under **common random numbers**: every policy faces the
identical realised world on seed *k*, so deltas are paired and variance collapses.
Headline numbers carry bootstrap 95% confidence intervals and a paired win rate.

A 2×2 ablation (rules vs LLM diagnosis × fixed-cap vs value-engine stopping) plus an
oracle arm decomposes the result: how much came from the value engine, how much from the
diagnosis, and how much headroom remains.

## Phases

`P0` scope and economics → `P1` foundation → `P2` calibration → `P3` world and frozen
datasets → `P4` harness and baselines → `P5` estimator ∥ `P6` diagnosis → `P7` value
engine → `P8` evidence package → `P9` submission.

P4 precedes P5–P7 deliberately: the scoreboard is built before the player, so iteration
runs against real numbers rather than impressions.
