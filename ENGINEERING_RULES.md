# Engineering rules for this repository

## What this is

A payment-recovery agent that optimises **net recovered value including customer cost**,
not success rate. Razorpay's core competency *is* success rate; a smarter retry engine
pitches them their own product, done worse. The thesis is one level up: recovery is a
sequential decision problem with costs, option value and a deadline, and sometimes the
correct action is to walk away from a recoverable payment.

The result the whole build exists to produce:

> The agent recovers **less money** than a max-recovery policy and produces **more net
> value** — reported side by side, with confidence intervals.

## Two rules that override everything else

1. **Code under `netvalue/agent/` may never import from `netvalue/world/`.**
   Also applies to `netvalue/policies/net_value.py`. The agent estimates the world's
   physics from observed outcomes (`agent/estimator.py`, fitted on `data/history.jsonl`);
   it never reads them. If this is violated the agent wins by construction and every
   reported number is a tautology. Enforced by `tests/test_boundary.py`.

2. **No test may be weakened, skipped or deleted to make a run pass.**
   If a test fails, either the code is wrong or the test encodes a decision that has
   genuinely changed — in which case change it deliberately and record it in
   `DECISIONS.md`. Never adjust a threshold to get green.

Unconstrained, an agent will eventually reach for ground truth or relax a threshold
because it makes the metric go up. That is the one bug in this project that looks exactly
like success.

## Stack

Python 3.11 · pydantic · SQLite · matplotlib · pytest · ruff · mypy strict.
One server-rendered HTML metrics page. Ugly is fine.

## Do not build

No auth. No multi-tenancy. No React. No Docker. No message queue. No model routing across
providers. **No plugin system, no provider abstraction, no runtime config DSL** — the
single typed config object in `world/config.py` is required and is not one of these. No
real gateway integration. No off-policy estimator. No adversarial world generator.

The last two are not cut because they are bad. They are the strongest ideas available and
they belong on the closing slide as "what I'd build next," specified precisely.

Unconstrained, an agent will build a platform, and Thursday will be spent deleting it.

## Where things live

| Path | Rule |
|---|---|
| `netvalue/world/` | Ground truth. Hidden physics. Never imported by the agent. |
| `netvalue/agent/` | Sees only `agent/observation.py`. Boundary-guarded. |
| `netvalue/policies/` | Strategies behind one `Policy` protocol. `max_recovery.py` is the sole allowlisted oracle. |
| `netvalue/eval/` | Harness, ledger, metrics, bootstrap, sweeps, ablation, report. Unrestricted. |
| `netvalue/llm/` | Client, response cache, schemas. Unrestricted. |
| `data/` | Frozen datasets and configs. **Never regenerated after the Phase 3 freeze.** |

## Non-negotiables

Cut these only if the project dies without cutting them. They are the credibility of every
number reported.

1. The `world/` ↔ `agent/` boundary and its test
2. The frozen dataset
3. The sensitivity sweep over the annoyance cost
4. Confidence intervals on the headline delta
5. The single untouched config B run

A smaller result that survives scrutiny beats a larger one that doesn't.

## Conventions

- Every economic or structural constant lives in `world/config.py`. Nothing hardcodes one.
- Every run records `config_hash`, dataset hashes, seeds and the git SHA in a manifest.
- LLM calls: temperature 0, pinned model id, cached on `sha256(prompt + model + params)`.
- Comment flags match `PHASE0_DECISIONS.md`: `[DESIGN]`, `[VERIFY-P2]`, `[DERIVED]`.
- `make check` (or `.\make.ps1 check` on Windows) before every commit.
- `make reproduce` must regenerate every number in the README from a clean clone. Each
  phase appends its stage; it is never allowed to go stale.
