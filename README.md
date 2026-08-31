# Net-Value Payment Recovery Agent

Recovery engines optimise **success rate**. This one optimises **net recovered value
including customer cost** — and sometimes decides that a recoverable payment is not worth
recovering.

> **The result this build exists to produce:** the agent recovers *less money* than a
> max-recovery policy and produces *more net value*, reported side by side with confidence
> intervals.

**Status: Phase 1 of 9 complete.** Foundation, typed config, boundary guard and CI are in
place. The world, harness, agent and evidence package land in Phases 3–8.

---

## Why this and not a smarter retry engine

Razorpay's core competency *is* payment success rate — routing, retry timing, acquirer
failover. A submission that retries more cleverly pitches them their own product, done
worse. The gap sits one level up.

A payment clawed back by pinging a customer three times can be value-destroying: ₹800
recovered, ₹2,000 of goodwill spent. Recovery is not a classification problem. It is a
sequential decision problem with costs, option value and a deadline.

The world is **subscription and e-mandate dunning**, deliberately: the customer is a stock
of future revenue, so contacting them has a measurable churn hazard and the annoyance cost
is structural rather than decorative.

## The thesis, in four numbers

Because both the value of a recovery and the cost of annoying a customer scale with
remaining lifetime value, LTV largely cancels — leaving an **amount-independent policy
boundary**: the probability of recovery that justifies sending contact *k*.

| Contact | Required P(recover) |
|---|---|
| 1st | 1.45% |
| 2nd | 4.55% |
| **3rd** | **12.73%** |
| 4th | 25.45% |

Nothing hardcodes these. They fall out of the cost model, and `tests/test_economics.py`
asserts they still do. A max-recovery policy sends every contact it is allowed to send;
contact 3 is where it destroys value it then books as a success-rate win.

## The premise is enforced, not asserted

**The error code must not give the cause away.** If `GW_05` mapped cleanly to
`insufficient_funds` there would be no inference problem and no reason for a model in the
system. Measured on the committed config:

```
H(cause)          2.608 bits
H(cause | code)   1.904 bits
I(cause; code)    0.705 bits      ceiling 1.000
```

| Code | P(code) | Top cause | Runner-up | H(cause\|code) |
|---|---|---|---|---|
| `GW_05` | 0.324 | insufficient_funds 60.9% | card_expired 11.1% | 1.926 |
| `GW_33` | 0.153 | afa_timeout 68.7% | insufficient_funds 13.4% | 1.481 |
| `GW_54` | 0.138 | route_degraded 39.9% | bank_outage 23.9% | 2.273 |
| `GW_11` | 0.134 | insufficient_funds 48.4% | risk_block 33.0% | 1.914 |
| `GW_91` | 0.127 | bank_outage 41.7% | insufficient_funds 26.8% | 2.149 |
| `GW_21` | 0.125 | card_expired 52.8% | mandate_dead 33.6% | 1.694 |

Three of these are ambiguous in ways that are *economically* expensive, not merely wrong:

- **`GW_11`** — a timed retry near payday, or a human escalation? Retrying into a risk
  block burns the mandate cycle cap for nothing.
- **`GW_21`** — a paid customer contact, or abandon? On a dead mandate a card-update
  request is pure annoyance with zero possible upside.
- **`GW_54`** — switch the acquirer, or wait out the outage?

`tests/test_ambiguity.py` fails if any code concentrates above 70%, if conditional entropy
drops below 1.40 bits, or if mutual information rises above 1.00 bits.

## The boundary

The whole submission rests on one claim: **the agent never reads the world's ground
truth.** It estimates the physics from observed historical outcomes; it does not import
them. Otherwise the value engine and the simulator share a model of
`P(recover | cause, intervention)`, the agent wins by construction, and every number is a
tautology.

```
world/  (ground truth)     │     agent/  (observation only)
hidden cause               │     error code, amount, rail, bank
recovery physics           │     attempt number, prior attempts
bank health, outages       │     customer history
                           │
   history.jsonl  ─────────┼───►  estimator.py  ──►  P-hat + uncertainty
   (outcomes, no causes)   │
                      enforced by tests/test_boundary.py
```

The guard walks the import graph with the AST, so it catches imports inside functions,
inside `TYPE_CHECKING` blocks and inside branches that never execute. One file is
allowlisted — `policies/max_recovery.py`, the success-rate ceiling, which is an oracle by
definition — and the test asserts that allowlist entries exist and carry a real
justification.

```bash
make boundary      # or:  .\make.ps1 boundary
```

## Quickstart

```bash
python -m pip install -e ".[dev]"

make check         # ruff + mypy strict + pytest        (.\make.ps1 check on Windows)
make config        # write data/config_a.json, print the ambiguity and economics tables
make boundary      # the credibility guard, on its own
make reproduce     # regenerate every number in this README from scratch
```

`make` is not required on Windows: `make.ps1` mirrors every target.

## Layout

```
netvalue/world/      ground truth and hidden physics — agent/ never imports this
netvalue/agent/      observation, estimator, belief, value engine, bounds
netvalue/policies/   four baselines and the net-value agent, one Policy protocol
netvalue/eval/       runner, ledger, metrics, bootstrap, sweeps, ablation, report
netvalue/llm/        client, response cache, structured-output schemas
data/                frozen configs and datasets — never regenerated after Phase 3
tests/               boundary, ambiguity, economics, config (live) + phase-gated stubs
```

## Documents

| File | What it holds |
|---|---|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | The split, the pipeline, and the decisions worth naming |
| [`PHASE0_DECISIONS.md`](PHASE0_DECISIONS.md) | Every economic and structural parameter, with its reasoning |
| [`DECISIONS.md`](DECISIONS.md) | One line per choice, with what would change my mind |
| [`CALIBRATION.md`](CALIBRATION.md) | Sourcing plan, per rail, with a `sourced \| chosen` flag on every row |
| [`CLAUDE.md`](CLAUDE.md) | Working rules, including the two that override everything else |
| [`razorpay-buildathon-plan_phases.md`](razorpay-buildathon-plan_phases.md) | The ten-phase build plan |

## Honesty

A simulator you wrote is a world you can rig. Five defences, stated before anyone asks:

1. **The agent cannot read the world.** Import-graph test, run as its own CI step.
2. **Calibrate per rail, never across.** NPCI BD/TD data is UPI; `card_expired` and
   `afa_timeout` are card failures. Every `CALIBRATION.md` row carries a rail, a source and
   a `sourced | chosen` flag. An admitted gap costs nothing; a mislabelled source costs
   everything.
3. **Publish the parameters.** `data/config_a.json` is committed, hashed into every run
   manifest, and CI fails if it drifts from the code.
4. **Publish the whole parameter space.** The Phase 8 sensitivity sweep reports net value
   across the full plausible range of annoyance costs and marks where the policy stops
   dominating — rather than defending one chosen value.
5. **Hold out a regime.** Tune on config A, evaluate once on config B, report whatever
   comes out.

### What calibration overturned

Phase 2 sourced every parameter and graded each `primary | secondary | chosen`
(14% / 67% / 19%). It broke four Phase 0 assertions:

| Asserted | The record says |
|---|---|
| 3 debits per mandate cycle is a network rule | India caps **no** retries per cycle; networks cap 10 (MC) / 15 (Visa) per **30 days** |
| Retry after 4 hours | Every retry needs a fresh **24h** pre-debit notification — 4h is non-compliant |
| `afa_timeout` is an auth timeout | AFA applies only **above ₹15,000**; the plan ladder tops out at ₹4,999, so it was unreachable |
| Both rails fail alike | UPI Autopay **8–15%**, card mandates **2–3%** |

The second mattered most: Phase 0 would have produced an agent whose optimal strategy was
illegal. It also changes the problem's shape — attempts are rate-limited to one per 24
hours, so the **expiry horizon binds rather than the attempt budget**, which makes the
finite-horizon decision in Phase 7 more load-bearing, not less.

**One parameter remains unsourceable and it is the one the result rests on.** No public
measurement exists of the incremental churn caused by the *k*-th dunning contact — the
industry publishes what dunning *recovers* and never what it *costs*. That asymmetry is
itself the point this project makes. The parameter is defended by the Phase 8 sensitivity
sweep rather than by a citation.

## Scope

Recurring rails only — card-on-file mandates and UPI Autopay. One-shot checkout is out of
scope on purpose, not by omission: without an ongoing customer relationship there is no
goodwill to spend, and the thesis has nothing to bite on.
