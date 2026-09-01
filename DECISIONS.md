# DECISIONS.md

One line per choice, written by hand, in your own words. Heavy agentic coding means fifty
choices you will not remember making, and the panel interviews on exactly those. This file
is the difference between defending your build and meeting it for the first time in the
room.

**Format:** `ID · date · the choice · why, in one sentence · what would change my mind.`

---

## Phase 0 — scope and economics · 2026-08-31

Full reasoning in [`PHASE0_DECISIONS.md`](PHASE0_DECISIONS.md); one-liners here.

- **001** · Two rails, both recurring; one-shot checkout excluded. *In one-shot there is no
  relationship to damage, so annoyance cost is decorative and the thesis collapses back
  into "smarter retry."* Would change if a judge reads the narrowing as scope avoidance
  rather than as a choice — mitigated by saying so out loud in the video.

- **002** · Simulation window 2026-03-05 → 04-05. *It is the shortest window containing
  both salary dates and the 31 March financial-year-end spike, so calendar effects are
  observable rather than asserted.*

- **003** · Seven causes; `mandate_dead` the only permanently unrecoverable class.
  *`card_expired` cannot be fixed by retrying but a card update fixes it, and that
  distinction is the whole reason the intervention set is richer than retry / don't.*

- **004** · Cause priors shaped to a 78/22 BD/TD split rather than chosen freely. *Using
  NPCI's own taxonomy as the constraint makes the priors answerable to something external.*
  → pending `[VERIFY-P2]`.

- **005** · `P(code|cause)` matrix with `GW_33` topping out at 68.7%. *The ceiling is 70%;
  the first draft had `afa_timeout` at 89% and the error code was giving the answer away,
  so mass was moved to `insufficient_funds` and `risk_block` on the physical grounds that
  an issuer decline during the auth step surfaces as an auth-stage code.*

- **006** · Ambiguity thresholds: max posterior 0.70, min `H(cause|code)` 1.40 bits, max
  `I(cause;code)` 1.00 bits. *Margins are thin on purpose — actual values are 0.687, 1.481
  and 0.705, so any drift in the world fires the test instead of quietly weakening the
  premise.*

- **007** · Plan ladder with 65% of volume at or below ₹149. *Uniform amounts would make
  almost everything worth recovering and empty the region the thesis lives in.*

- **008** · Recovery value carries retained LTV, not just the invoice. *A failed renewal
  that is never recovered is involuntary churn; modelling it as a missed ₹99 produces a
  degenerate agent that abandons everything, which is the mirror of the failure being
  guarded against.*

- **009** · Annoyance derived as `Δchurn(k) × LTV_remaining`, convex in contact index.
  *It is the single most attackable number in the project, so it is not a number — it is a
  model with a churn hazard and an LTV, both sourceable.* This is the parameter the Phase 8
  sensitivity sweep exists to neutralise.

- **010** · Human escalation costs ₹85 and has **no minimum-amount precondition**.
  *Whether a human should touch a ₹99 recovery is exactly the judgment the value engine
  exists to make; a hardcoded floor would steal that decision from the thesis.*

- **011** · Expiry horizons 21d card / 14d UPI. → pending `[VERIFY-P2]`.

- **012** · `max_attempts = 6`, not 3. *Three would hand the agent the naive baseline's
  policy by construction; six is a ceiling the agent chooses within.*

- **013** · The observable schema redeclares its own enums instead of importing world's.
  *So the boundary contract stands alone and the guard can never pass vacuously.*

---

## Phase 1 — foundation · 2026-08-31

- **014** · Config serialised as JSON, not the YAML named in the plan. *JSON gives
  canonical key-sorted serialisation for hashing with no extra dependency; the run manifest
  needs a stable hash more than the file needs to be pretty.*

- **015** · Boundary enforced by AST import-graph walk, not a runtime import check.
  *A runtime check misses imports inside functions, inside `TYPE_CHECKING` blocks, and
  inside branches that never execute — all three of which are exactly how this rule would
  get broken by accident.*

- **016** · `policies/max_recovery.py` is the sole entry on the boundary allowlist, with a
  written justification the test asserts is non-trivial. *Baseline 3 is an oracle by
  definition — being one is the entire point of the success-rate ceiling — but a blanket
  exemption for `policies/` would quietly reopen the boundary for the net-value agent.*

- **017** · `delta_churn_beyond` plateaus for contacts 4+, and a test asserts the bounds
  never permit a contact that lands on it. *A reachable decision must never rest on an
  unexamined fallback number.*

- **018** · Ambiguity and economics tests run against the config from Phase 1, before the
  world exists. *The premise is mechanical from the first commit rather than checked once
  by hand and assumed thereafter.*

- **019** · Phase-gated tests are committed as skipped stubs with markers. *The test plan
  is visible from the start instead of being invented after the code exists, which is how
  test suites end up ratifying whatever was built.*

- **020** · CI fails if `data/config_a.json` is stale relative to the code. *Publishing the
  parameters is one of the five honesty moves, and a published file that silently drifts
  from the code is worse than not publishing it.*

- **021** · `make.ps1` mirrors the Makefile target-for-target. *`make` is absent on the dev
  machine and present in CI; without a shim the documented interface would be one nobody
  actually runs locally.*

---

## Phase 2 — calibration · 2026-08-31

Full sourcing with grades and URLs in [`CALIBRATION.md`](CALIBRATION.md).

- **022** · `min_inter_attempt_hours` raised 4 → 24, enforced by a validator. *RBI's 2026
  e-mandate framework requires a fresh pre-debit notification at least 24h before every
  attempt, so the Phase 0 value would have produced an agent whose optimal strategy was
  illegal.* Would change only if the framework is amended — this is a regulatory floor,
  not a tuning knob.

- **023** · `max_debits_per_mandate_cycle = 3` relabelled from network rule to merchant
  policy, and a genuine sourced cap added alongside it (`network_retry_cap_per_30d = 10`).
  *India caps no number of retries per cycle; the card networks cap 10 (Mastercard) and 15
  (Visa) per 30 days. Asserting a made-up number as a regulator's rule is worse than
  admitting a choice.* Kept at 3 because the economics, not the cap, should be what stops
  the agent.

- **024** · Retry constraint is temporal, not a count. *This follows from 022 and 023 and
  it changes the problem: attempts are rate-limited to one per 24h, so the expiry horizon
  binds rather than the attempt budget.* Makes the finite-horizon DP in Phase 7 more
  load-bearing, not less.

- **025** · `afa_timeout` rescoped to include the pre-debit opt-out. *AFA is only required
  above ₹15,000 and the plan ladder tops out at ₹4,999, so the cause as originally written
  was physically unreachable in my own world.* The 2026 framework attaches an opt-out to
  every pre-debit notification at any amount, which is a real and more common failure mode.

- **026** · Added `base_failure_rate_by_rail` (card 2.5%, UPI 11.5%). *Published rates are
  2–3% against 8–15%; UPI Autopay is stateless per debit while card mandates are
  bank-managed. A world where both rails fail alike would erase a distinction the agent
  should be exploiting.*

- **027** · `p_involuntary_churn` kept at 0.55, now anchored rather than invented. *30–50%
  of involuntary-churn customers reactivate unaided, so 50–70% do not; 0.55 sits inside
  that band toward the optimistic end.* Would move if a subscription-specific figure
  surfaces.

- **028** · `card_expired` prior kept at 0.12 despite a conflicting 25–30% figure. *The 12%
  source is subscription-specific; the higher one is general card decline data. The
  conflict is recorded in CALIBRATION.md rather than resolved, so a reviewer who knows the
  higher number can see it was considered.*

- **029** · The merchant-side UPI failure mix is deliberately **not** used to set the BD/TD
  split. *It measures merchant-observed failures rather than NPCI's BD/TD classification,
  and taken naively implies TD near 50% against a published ~0.8%. Two different quantities
  wearing similar labels is exactly the trap the calibration file exists to prevent.*

- **030** · Sources graded `primary | secondary | chosen` rather than cited flatly. *Most of
  the economics comes from vendor benchmark posts, which are real data but self-published;
  presenting those at the same weight as an NPCI circular would overstate the ground I
  actually stand on.* 14% primary, 67% secondary, 19% chosen.

- **031** · Per-bank technical decline left `chosen`. *The NPCI BD/TD page returns HTTP 403
  to automated fetches. The spread is built into the world regardless, but the specific
  numbers are invented and are labelled so.* Upgrade by pulling the page manually before
  the video.

## Phase 3 — world and frozen datasets · 2026-09-01

- **032** · Every random draw is keyed by `(transaction_id, attempt_index, purpose)` rather
  than taken from one sequential generator. *Two policies that make different choices must
  face the same world; with a sequential stream a policy that retried twice would shift
  every later draw and the paired comparison in Phase 4 would be impossible.* This is the
  single most load-bearing implementation decision in the phase.

- **033** · **The ambiguity guarantee was being measured wrong, and it was actually
  breached.** *A cause's prior is conditional on that cause being possible, but two causes
  are card-only, so the generator renormalises on the UPI rail. Measured on the effective
  prior, `GW_33` carried 70.4% of its mass on one cause against a 70% ceiling — while the
  raw-prior calculation reported a comfortable 68.7%.* Added `effective_cause_prior()` and
  rewired every analytic and validator through it. A guarantee checked against a quantity
  the world does not have is not a guarantee.

- **034** · Fixed the breach by cutting `P(GW_33 | afa_timeout)` 0.70 → 0.60 rather than
  raising the ceiling. *The 70% constraint predates the measurement; moving it to fit the
  world would be exactly the "weaken a test to get green" failure `CLAUDE.md` forbids.*
  Freed mass went to `GW_05` and `GW_54`, which an incomplete authorisation plausibly
  surfaces as. Max mass is now 64.0%.

- **035** · BD target moved 0.78 → 0.80. *Not tuning: the effective prior gives 80.7%,
  which sits directly on the published ~80/20 split. The 0.78 figure was fitted to raw
  priors describing a population the generator never produces.*

- **036** · Test asserts the top-two *set* for the three loaded ambiguities, not their
  order. *Ordering is an artifact of the rail mix — `card_expired` is card-only so it
  carries less effective mass than `mandate_dead` despite being likelier on the card rail
  — and flips under legitimate changes. Both carrying real mass is the property that
  matters.* `GW_21` is now contested 43/40, more ambiguous than before.

- **037** · Retries, delayed retries, scheduled retries and route switches share one
  physics function. *They are the same physical act — presenting a debit at a moment in
  time — differing only in when and where they land. Giving each its own hand-tuned success
  rate would let me quietly encode the answer I wanted.*

- **038** · Only ~55% of expired cards show a visibly past stored expiry. *Reissues change
  the number while the stored date still looks fine. Without that overlap the expiry field
  would resolve `GW_21` on sight and the most economically loaded confusion in the world —
  a paid contact against an abandon — would collapse into a lookup.*

- **039** · Customer history is generated cause-conditionally. *The error code is
  deliberately uninformative, so if nothing else discriminated, diagnosis would be guessing
  from the prior and the model layer would be decoration. The signal has to live somewhere,
  and history is where a real analyst would look.*

- **040** · The history logging policy explores across the whole intervention set. *A
  policy that only retried would leave the estimator blind about contacts and escalations
  — blind in a direction that happens to favour my thesis, which is exactly the convenient
  gap a reviewer should be able to rule out.* Asserted by a test requiring ≥100 logged
  actions and ≥1 success per intervention.

- **041** · History uses a different population and seed from `dataset_a`. *Training the
  estimator on transactions it is later scored against would inflate it for free.* Asserted
  by a disjointness test.

- **042** · Config B differs **structurally**, not in difficulty. *A uniformly harder world
  would only show the agent degrades, which is uninteresting. Correlated multi-bank outage,
  an unseen error code, and inverted segment response each make a rule learned on A
  actively wrong on B — attacking the value engine and the diagnoser separately.*

- **043** · Re-froze the datasets once, with `--force`, after the 033 fix. *The freeze
  discipline exists to stop re-rolling after seeing agent scores. No agent exists yet and
  no number had been reported; the regeneration was forced by a correctness fix to a
  metric, not by a disappointing result.* This is the only re-freeze; there will not be
  another.

- **044** · `generate_datasets.py` refuses to run when a manifest exists unless `--force`.
  *The freeze needs a mechanism, not just a rule in a document — the failure mode is
  regenerating absent-mindedly on a Thursday.*

## Template for later phases

```
- **NNN** · YYYY-MM-DD · <the choice> · *<why in one sentence>* · <what would change my mind>
```

Ten minutes a day. Write it when the choice is made, not on Friday.
