# Net-Value Payment Recovery Agent

Recovery engines optimise **success rate**. This one optimises **net recovered value
including customer cost** — and sometimes decides that a recoverable payment is not worth
recovering.

> **The result, measured:** against a policy that maximises success rate regardless of
> cost, this agent produces **₹693,363 more net value** (95% CI [+167,357, +1,221,923])
> across 30 paired replications — on **half the customer contacts** and 39% of the
> goodwill cost.
>
> The project predicted it would get there by recovering *less* money. It does not: it
> recovers ₹169,471 more as well. The prediction is left here rather than quietly edited
> out, because what a build set out to show and what it measured are different things and
> only one of them is evidence.

**Status: Phase 7 of 9 complete.** The agent exists and beats the success-rate ceiling.
The sensitivity sweep, the ablation table and the single held-out run land in Phase 8.

## The result

30 replications x 400 transactions, config A. Every policy faces the identical realised
world at each replication index, so the deltas are paired.

| Policy | Net value ₹ | Gross recovered ₹ | Recovery | Contacts | Annoyance ₹ |
|---|---:|---:|---:|---:|---:|
| `no_retry` — the floor | 0 | 0 | 0.0% | 0 | 0 |
| `naive_retry` — 3 attempts, 24h apart | 15,416,121 | 15,431,789 | 50.0% | 0 | 0 |
| **`max_recovery` — the success-rate ceiling** | 16,964,195 | 18,180,751 | 58.2% | 10,767 | 916,693 |
| **`net_value` — this agent** | **17,657,558** | 18,350,222 | 59.3% | 5,409 | **355,899** |
| `max_recovery_oracle` — perfect diagnosis | 19,047,829 | 19,357,303 | 64.7% | 3,071 | 205,491 |

**+₹693,363 against the ceiling, 95% CI [+167,357, +1,221,923], sign resolved.** Half the
contacts and 39% of the customer-goodwill cost.

### The predicted sentence was wrong, and the report says so

This project set out to show the agent recovering *less money* while producing *more net
value*. **It does not.** It recovers **₹169,471 more** and nets ₹693,363 more, by spending
₹560,794 less on goodwill. It wins on both axes rather than trading one for the other.

That is a stronger result and a different claim, so `thesis_line` reads the actual numbers
and names which of three outcomes occurred — including the one where the thesis fails.
A report that can only describe the hoped-for result is not a measurement.

**The paired win rate is 51.4%.** The agent is better *on average*, not *typically*: the
gain is carried by large wins on some transactions. Reported beside the mean because a
judge would find it.

### Where the option value comes from

Stopping is finite-horizon backward induction over a belief state, not a one-step
threshold. A greedy rule cannot represent option value at all — a cheap wait whose entire
worth is that it unlocks a later action scores zero to it. Mean Q per decision is **₹806
greedy against ₹1,486 at depth 4**, and the continuation term *is* the option value: it
falls out of the recursion rather than being a term anyone invented and tuned.

Depth matters discontinuously. Depths 2 and 3 are indistinguishable (₹565,774 vs
₹565,767); depth 4 jumps to ₹580,487 — the first depth that can see a customer contact
*after* the debit budget is spent. `tests/test_dp.py` checks the recursion against an
independent brute-force enumeration.

## Diagnosis is scored in rupees, not accuracy

A confusion matrix weights every mistake the same. Here they differ by **50×**:

| True cause | Diagnosed as | Net value destroyed |
|---|---|---:|
| `bank_outage` | `risk_block` | ₹575 |
| `card_expired` | `mandate_dead` | ₹350 — the whole recovery, abandoned |
| `mandate_dead` | `card_expired` | ₹11 — one wasted contact |

13 of 42 possible confusions cost under ₹5; the worst costs ₹575. An accuracy figure
reports those identically, so the metric is **regret in rupees**, computed from the actual
economics of the action each diagnosis implies.

| Diagnoser | Accuracy | Top-2 | Mean regret ₹ | Confidence ECE |
|---|---:|---:|---:|---:|
| `rules` — the ablation floor | 65.5% | 81.8% | 35.3 | 0.206 |
| `llm` | *pending — see below* | | | |
| `oracle` — the ceiling | 100.0% | 100.0% | 0.0 | 0.000 |

**The rules arm is deliberately a fair opponent.** It uses every observable cue the world
puts there — a passed expiry, a habitual late payer, outreach never answered, a mandate
long dead, the rail's own constraints — and reaches 65.5%. If the model only beat a bad
rule table, the AI-judgment claim would be worth nothing.

**The more interesting number is its confidence ECE of 0.206.** The rules arm is a decent
classifier and a *bad probability source*: it says 36% and is right 11% of the time, says
83% and is right 97%. The value engine consumes confidence directly, so that miscalibration
costs real money — and it has nothing to do with accuracy. That is the sharpest available
case for a model in this slot.

### The LLM arm is built, priced, and not yet run

Nothing calls the API without `--live`. A full pass over the 400-transaction evaluation set:

| Model | Provider | Cost |
|---|---|---:|
| `claude-opus-5` | Anthropic | $3.29 |
| `claude-sonnet-5` | Anthropic | $1.32 |
| `grok-4.6` | xAI | $0.90 |
| `grok-4.5` | xAI | $0.90 |
| `claude-haiku-4-5` | Anthropic | $0.66 |

Or run it for **nothing at all** — on Gemini's free tier, or on your own GPU:

```bash
# Gemini free tier: throttled to stay inside the per-minute limit
python scripts/run_diagnosis.py --config a --live --model gemini-2.5-flash

# fully offline, no key, no network
ollama pull qwen2.5:7b-instruct
python scripts/run_diagnosis.py --config a --live --model local/qwen2.5:7b-instruct
```

The provider follows from the model id: `claude-*` → `ANTHROPIC_API_KEY`, `grok-*` →
`XAI_API_KEY`, `gemini-*` → `GEMINI_API_KEY`, and `local/*` → an OpenAI-compatible server
on localhost (Ollama, llama.cpp, vLLM, LM Studio; override with `LOCAL_LLM_BASE_URL`).
All four sit behind one structured-output contract, so nothing downstream can tell which
produced a posterior — the property that keeps the ablation fair.

**Free tiers are rate-limited and capped daily**, so the runner paces itself
(`--max-live-calls` bounds a session) and every completed response is cached before the
next one starts. A run that stops on a quota resumes where it left off rather than
starting over, and all arms are then scored on the same subset so a partial pass stays
comparable. `--list-models` asks the provider what a key can actually reach.

Paid **once** — every response is cached on `sha256(model + params + prompt)` and the cache
is committed, so re-runs are free, deterministic, and work with no network. That also means
the demo cannot fail because an API is unhealthy at the moment you press play.

## The estimator — the agent learns the physics, it does not read them

The value engine needs `P(success | action)`. It must not get that from the simulator —
then agent and world would share one model and the agent would win by construction. So it
gets what a real payments team has: a log of what a previous, unintelligent system did and
what happened (`history.jsonl`, 6,172 actions, **no cause field**), and it estimates from
that.

A hierarchical beta-binomial along a six-rung backoff ladder, from a nine-feature cell
down to the global rate; sparse cells borrow strength from coarser ones, weighted by
κ=20 pseudo-observations chosen by validation log-loss. Validated on a held-out quarter of
the log, split by transaction:

| Model | Brier ↓ | Log-loss ↓ | ECE ↓ |
|---|---:|---:|---:|
| Global rate | 0.1605 | 0.5021 | 0.0216 |
| Per-intervention rate | 0.1559 | 0.4822 | 0.0225 |
| **Estimator** | **0.1274** | **0.3989** | **0.0328** |

**Calibration is the gate, not accuracy.** The value engine multiplies these probabilities
by rupees, so an estimator that says 70% and is right 50% of the time makes the agent
spend money it should not — and no accuracy metric would show it. ECE ≤ 0.05 and
Brier-beats-the-base-rate are enforced by the fit script, by a test, and by CI on every
push.

Two things the estimator demonstrably *learned* rather than was told: that a debit is
likelier to clear near payday, and that retries decay with attempt number. Both are
asserted by tests, and neither number exists in any file the agent can read.

It is **cause-agnostic by design** — the log never knew why a payment failed, so neither
can the estimator. That is the situation every real dunning team is in. It also means the
Phase 7 belief update cannot get `P(fail | cause, action)` from here; it will come from the
diagnoser's own model.

## The scoreboard, before the agent exists

30 replications × 400 transactions, config A. Every policy faces the identical realised
world at each replication index, so the deltas are paired.

| Policy | Net value ₹ | Gross recovered ₹ | Recovery | Contacts | Annoyance cost ₹ |
|---|---:|---:|---:|---:|---:|
| `no_retry` — the floor | 0 | 0 | 0.0% | 0 | 0 |
| `naive_retry` — 3 attempts, 24h apart | 15,416,121 | 15,431,789 | 50.0% | 0 | 0 |
| **`max_recovery` — the success-rate ceiling** | **16,964,195** | **18,180,751** | 58.2% | 10,767 | **916,693** |
| `max_recovery_oracle` — perfect diagnosis | 19,047,829 | 19,357,303 | 64.7% | 3,071 | 205,491 |

**The two numbers the agent must produce:** beat **₹16,964,195** of net value while
recovering less than **₹18,180,751** gross.

The ceiling burns **₹916,693 of annoyance cost** to buy its extra recoveries. That is the
headroom the thesis exploits — and note the oracle row, which recovers *more* while
spending *less* on annoyance, because knowing the cause means only contacting when a
contact is the right lever. Diagnosis quality reduces cost, it does not merely raise
recovery.

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
H(cause)          2.526 bits
H(cause | code)   1.898 bits
I(cause; code)    0.627 bits      ceiling 1.000
```

Measured on the **effective** cause prior — the distribution the generator actually
realises. Phase 3 found this mattered: two causes are card-only, so the UPI rail
renormalises over what remains, and the raw-prior calculation had `GW_33` at a comfortable
68.7% while the world was really at **70.4%, over the ceiling**. A guarantee checked
against a quantity the world does not have is not a guarantee. The fix was to lower the
offending mass, not to raise the ceiling.

| Code | P(code) | Top cause | Runner-up | H(cause\|code) |
|---|---|---|---|---|
| `GW_05` | 0.337 | insufficient_funds 63.6% | risk_block 9.5% | 1.853 |
| `GW_33` | 0.155 | afa_timeout 64.0% | insufficient_funds 16.9% | 1.573 |
| `GW_11` | 0.146 | insufficient_funds 49.0% | risk_block 33.4% | 1.866 |
| `GW_54` | 0.128 | bank_outage 28.5% | route_degraded 27.9% | 2.369 |
| `GW_91` | 0.127 | bank_outage 45.8% | insufficient_funds 29.5% | 2.043 |
| `GW_21` | 0.107 | mandate_dead 43.3% | card_expired 40.0% | 1.821 |

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

## The frozen world

Three datasets, hash-pinned in `data/manifest.json` alongside both config hashes, every
seed and the git SHA. `tests/test_determinism.py` re-runs the generator and asserts the
output is **byte-identical** to the freeze, so the world is recoverable from source rather
than merely unedited.

| File | Rows | Role |
|---|---|---|
| `dataset_a.jsonl` | 400 | Tuning regime |
| `dataset_b.jsonl` | 400 | Held-out regime — run once, on Friday, untouched |
| `history.jsonl` | 6,172 | Estimator training split: observed outcomes, **no causes** |

**54% of config A is recoverable but not worth recovering** at a third contact — computed
as a property of the dataset before any agent exists. If that number were near zero the
annoyance cost would not be biting and the thesis would have nothing to work with.

Config B differs *structurally*, not in difficulty — a uniformly harder world would only
show the agent degrades. Each difference makes a rule learned on A actively wrong on B:

- **A correlated four-bank outage.** On A, "wait a few hours or route around it" is a good
  rule. Here several banks fail together for most of a day and waiting burns horizon the
  mandate does not have.
- **An unseen error code** (`GW_99`, 12%). No prior, no estimator cell. Whether the system
  degrades gracefully or invents a confident diagnosis is the thing under test.
- **Inverted segment response.** Engaged customers stop answering and dormant ones start,
  so a contact policy fitted on A targets exactly the wrong people.

Two data-level boundary guarantees back the import guard: `history.jsonl` is asserted to
contain no cause field, and its population is asserted disjoint from the evaluation set.

## Quickstart

```bash
python -m pip install -e ".[dev]"

cp .env.example .env      # then paste whichever API key you have (optional)

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
| [`ENGINEERING_RULES.md`](ENGINEERING_RULES.md) | Working rules, including the two that override everything else |

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
