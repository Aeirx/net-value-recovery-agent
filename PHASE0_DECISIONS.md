# Phase 0 — Scope Lock and Economics

**Status:** complete. Exit criterion satisfied (§9).
**Revised 2026-08-31 by Phase 2 calibration** — four assertions here were overturned by the
sourced record. Corrections are marked **[P2-CORRECTED]** inline; see `CALIBRATION.md`.
**Code written:** none, by design.
**Feeds:** `CLAUDE.md`, `ARCHITECTURE.md`, `DECISIONS.md`, `world/config.py` (P1), `CALIBRATION.md` (P2).

Every number below is one of three kinds, flagged inline:

| Flag | Meaning |
|---|---|
| **[DESIGN]** | A modelling choice. Mine to defend. Not claimed to be an external fact. |
| **[VERIFY-P2]** | Asserted as a real-world fact. **Must be sourced in Phase 2 or changed.** Do not ship unverified. |
| **[DERIVED]** | Computed from other parameters in this document. Recompute if inputs move. |

---

## 1. World scope

### DECISION-001 — Two rails, both recurring. One-shot checkout excluded.

| Rail | Share | What it is |
|---|---|---|
| `card_mandate` | 65% **[DESIGN]** | Card-on-file e-mandate — subscription renewal debits |
| `upi_autopay` | 35% **[DESIGN]** | UPI Autopay mandate debits |

**Why recurring-only.** In one-shot checkout the merchant has no relationship to damage, so customer-annoyance cost is decorative and the thesis collapses back into "smarter retry." In subscription dunning the annoyance cost is *structural*: the customer is a stock of future revenue, contacting them has a measurable churn hazard, and recovering ₹99 by burning goodwill is a genuinely bad trade. The economics do the arguing for me.

**Secondary benefits.** Small recurring amounts give the fat left tail the thesis needs. LTV is knowable because tenure is observable. Razorpay ships a Subscriptions product, so it lands inside their stack rather than beside it.

**Cost of the choice.** Narrower than "payment recovery" in general. Stated up front in the README and the video as a deliberate narrowing, not an oversight.

**The two rails differ economically, which matters.** UPI Autopay carries zero MDR **[VERIFY-P2]**, so a recovered UPI renewal is worth strictly more net than an identical card renewal. The agent should — and will — behave differently by rail. This is free evidence that the value engine is doing real work.

### DECISION-002 — Simulation window: 2026-03-05 → 2026-04-05 IST (31 days)

**[DESIGN]** Chosen so the window contains all three temporal structures at once: the 1st-of-month salary spike, the 7th-of-month salary spike, and the 31 March financial-year-end bank closing spike **[VERIFY-P2]**. A shorter window would force the calendar effects to be asserted rather than observed.

---

## 2. Causes

### DECISION-003 — Seven causes, per-rail applicability, BD/TD mapped per rail

| Cause | Class | `card_mandate` | `upi_autopay` | Recoverable by retry? |
|---|---|---|---|---|
| `insufficient_funds` | BD | ✓ | ✓ | Yes — timing-dependent |
| `card_expired` | BD | ✓ | — | **No.** Only `request_card_update` |
| `risk_block` | BD | ✓ | ✓ | **No.** Only `escalate_to_human` |
| `mandate_dead` | BD | ✓ | ✓ | **Never. Permanently unrecoverable.** |
| `afa_timeout` | BD | ✓ | ✓ | Yes — immediate retry often works |
| `bank_outage` | TD | ✓ | ✓ | Yes — after the window |
| `route_degraded` | TD | ✓ | — | Only via `switch_route_and_retry` |

**Naming.** `afa_timeout` replaces v3's `3ds_timeout` and `mandate_dead` replaces `invalid_card`, because in a recurring world the failure is an additional-factor-authentication timeout on a high-value debit, and a dead instrument is usually a revoked mandate or closed account rather than a bad card number. Same seven slots, correct vocabulary.

**Permanently unrecoverable:** `mandate_dead` only — the honest-exception class. `card_expired` is unrecoverable *by retry* but recoverable by card update; that distinction is the whole reason the intervention set is richer than "retry / don't retry."

**`route_degraded` and `card_expired` are card-only.** A merchant cannot switch acquirer for a UPI Autopay debit, and there is no card to expire. This makes intervention validity rail-dependent, which enriches the diagnosis problem at zero extra cost.

**BD/TD note.** `afa_timeout` is classified BD (user did not complete authentication). A minority of cases are genuinely TD (challenge never delivered). Classified BD to match how NPCI would count it **[VERIFY-P2]**, with the caveat recorded here so it can be defended.

### DECISION-004 — Cause priors

**[DESIGN]**, shaped to hit an ~78/22 BD/TD split **[VERIFY-P2 against current-month NPCI figures]**.

| Cause | Prior | Class |
|---|---|---|
| `insufficient_funds` | 0.34 | BD |
| `afa_timeout` | 0.15 | BD |
| `card_expired` | 0.12 | BD |
| `risk_block` | 0.10 | BD |
| `mandate_dead` | 0.07 | BD |
| `bank_outage` | 0.11 | TD |
| `route_degraded` | 0.11 | TD |
| | **1.00** | **BD 0.78 / TD 0.22** |

---

## 3. Error-code ambiguity matrix

This table is the load-bearing constraint of the entire project. If the error code identifies the cause, there is no inference problem, no reason for an LLM, and a reviewer sees it in ten seconds.

### DECISION-005 — Six codes, `P(code | cause)`

**[DESIGN]** Rows sum to 1.

| Cause \ Code | `GW_05`<br>declined by issuer | `GW_11`<br>do not honour | `GW_21`<br>instrument invalid | `GW_33`<br>auth not completed | `GW_54`<br>upstream timeout | `GW_91`<br>issuer unavailable |
|---|---|---|---|---|---|---|
| `insufficient_funds` | 0.58 | 0.19 | 0.02 | 0.06 | 0.05 | 0.10 |
| `card_expired` | 0.30 | 0.05 | 0.55 | 0.00 | 0.05 | 0.05 |
| `risk_block` | 0.32 | 0.44 | 0.04 | 0.12 | 0.04 | 0.04 |
| `mandate_dead` | 0.20 | 0.08 | 0.60 | 0.00 | 0.05 | 0.07 |
| `afa_timeout` | 0.10 | 0.03 | 0.02 | 0.70 | 0.13 | 0.02 |
| `bank_outage` | 0.12 | 0.03 | 0.01 | 0.06 | 0.30 | 0.48 |
| `route_degraded` | 0.15 | 0.05 | 0.02 | 0.08 | 0.50 | 0.20 |

### Induced posteriors `P(cause | code)` — **[DERIVED]**

| Code | `P(code)` | Top cause | 2nd | 3rd | `H(cause\|code)` |
|---|---|---|---|---|---|
| `GW_05` | 0.3239 | insufficient_funds **0.609** | card_expired 0.111 | risk_block 0.099 | 1.93 bits |
| `GW_11` | 0.1335 | insufficient_funds **0.484** | risk_block 0.330 | card_expired 0.045 | 1.89 bits |
| `GW_21` | 0.1251 | card_expired **0.528** | mandate_dead 0.336 | insufficient_funds 0.054 | 1.71 bits |
| `GW_33` | 0.1528 | afa_timeout **0.687** | insufficient_funds 0.134 | risk_block 0.079 | 1.48 bits |
| `GW_54` | 0.1380 | route_degraded **0.399** | bank_outage 0.239 | afa_timeout 0.141 | 2.27 bits |
| `GW_91` | 0.1267 | bank_outage **0.417** | insufficient_funds 0.268 | route_degraded 0.174 | 2.15 bits |

`H(cause)` = 2.61 bits · `E[H(cause | code)]` = 1.90 bits · **`I(cause; code)` = 0.71 bits**

The code carries real signal and leaves 1.9 bits of uncertainty. It is nowhere near sufficient. Customer history has to do the rest — which is exactly the design intent, now quantified rather than hoped for.

### The three ambiguities that matter economically

These are the ones where the wrong call is expensive, not just wrong:

1. **`GW_11` — insufficient_funds (48%) vs risk_block (33%).** Interventions diverge completely: a timed retry near the salary date versus a human escalation. Retrying into a risk block burns the mandate cycle cap for nothing.
2. **`GW_21` — card_expired (53%) vs mandate_dead (34%).** Card update is a paid customer contact; on a dead mandate it is pure annoyance with zero possible upside. Getting this wrong either burns goodwill on a corpse or abandons a live subscription.
3. **`GW_54` — route_degraded (40%) vs bank_outage (24%).** Switch the route, or wait out the window. Switching during an outage wastes an attempt; waiting on a degraded route wastes the horizon.

### DECISION-006 — Ambiguity assertions for `tests/test_ambiguity.py` (P3)

```
for every code c:
    max_j P(cause_j | c)   <= 0.70      # actual max: 0.687 (GW_33)
    H(cause | c)           >= 1.40 bits # actual min: 1.48  (GW_33)
I(cause; code)             <= 1.00 bits # actual:     0.71
```

Margins are deliberately thin on `GW_33`. If the generator drifts, the test fires. That is the point.

---

## 4. Cost model

### DECISION-007 — Amount distribution

**[DESIGN]** Discrete plan ladder, heavy small-ticket mass. Uniform amounts would make almost everything worth recovering and empty the region the thesis lives in.

| Plan | Amount | Share |
|---|---|---|
| Basic | ₹99 | 42% |
| Standard | ₹149 | 23% |
| Plus | ₹299 | 18% |
| Premium | ₹599 | 11% |
| Annual Lite | ₹1,499 | 4% |
| Annual Pro | ₹4,999 | 2% |

Mean ₹355 **[DERIVED]** · median ₹149 · 65% of volume at or below ₹149.

### DECISION-008 — Value of a recovery is LTV-bearing, not just the invoice

A failed renewal that is never recovered is **involuntary churn**, not a missed ₹99. Modelling recovery value as the invoice alone would make almost nothing worth pursuing and produce a degenerate agent that abandons everything.

```
V_recover = amount × (1 − mdr) + p_involuntary_churn × LTV_remaining
```

| Parameter | Value | Flag |
|---|---|---|
| `mdr` — card_mandate | 2.0% | [VERIFY-P2] |
| `mdr` — upi_autopay | 0.0% | [VERIFY-P2] |
| `p_involuntary_churn` — churn probability if the renewal is never recovered | 0.55 | [VERIFY-P2] |
| `LTV_remaining` | `amount × expected_remaining_cycles` | [DERIVED] |

`expected_remaining_cycles` by segment **[DESIGN]**: engaged 14 · lapsed 7 · dormant 4.

### DECISION-009 — Annoyance cost is derived, escalating and convex

**Never a chosen rupee figure.** The single most attackable number in the project, so it is not a number — it is a model:

```
annoyance_k = Δchurn_k × LTV_remaining
```

`Δchurn_k` = incremental **voluntary** churn hazard from the k-th customer contact **[VERIFY-P2 against published dunning/retention research]**:

| Contact | `Δchurn_k` | Cost on ₹99 / 11-cycle (`LTV_rem` ₹1,089) | Cost on ₹4,999 / 2.2-cycle (`LTV_rem` ₹10,998) |
|---|---|---|---|
| 1st | 0.008 | ₹8.71 | ₹87.98 |
| 2nd | 0.025 | ₹27.23 | ₹274.95 |
| 3rd | 0.070 | ₹76.23 | ₹769.86 |
| 4th+ | 0.140 | ₹152.46 | ₹1,539.72 |

Convex escalation is the realistic shape — dunning fatigue is not linear — and it is what makes stopping a genuine decision rather than a formality. Annoyance scales with LTV, so **high-value customers are more expensive to annoy**, which produces non-obvious policy.

### DECISION-010 — Attempt costs

**[DESIGN]**

| Cost | Value | Notes |
|---|---|---|
| Retry (any timing variant) | ₹0.60 | Gateway auth request + infra |
| Route switch retry | ₹0.90 | Switch overhead |
| Card-update request | ₹0.60 | Comms only — annoyance is separate |
| Human escalation | ₹85.00 | ~7 min at ₹730/hr fully loaded **[VERIFY-P2]** |
| Abandon | ₹0.00 | |

### DECISION-011 — Expiry horizon

**[DESIGN, mechanism VERIFY-P2]** Measured from first failure, after which the mandate is force-cancelled and the subscription is lost.

- `card_mandate`: 21 days
- `upi_autopay`: 14 days

---

## 5. The crossover thresholds — proof the annoyance cost is load-bearing

**[DERIVED]** For small-ticket plans the invoice term is dominated by the LTV term, so `V_recover ≈ 0.55 × LTV_rem`. Contact *k* is worth sending iff:

```
P(recover from contact k) × V_recover  >  flat_cost + Δchurn_k × LTV_rem
```

`LTV_rem` cancels almost entirely, leaving a clean, amount-independent policy boundary:

| Contact | Required `P(recover)` to justify |
|---|---|
| 1st | **1.5%** |
| 2nd | **4.5%** |
| 3rd | **12.7%** |
| 4th | **25.5%** |

**This is the thesis, in four numbers.** Contact 1 is nearly free. Contact 2 is roughly break-even. **Contact 3 is value-destroying unless the recovery chance exceeds ~13%** — which it does for engaged segments and does not for dormant ones. A max-recovery policy sends contact 3 to everything; the net-value agent sends it to a minority. The boundary is not hardcoded anywhere — it falls out of the economics.

### Population-level illustration of the divergence

Take 1,000 `GW_21` transactions (card_expired 53% / mandate_dead 34%), blended `LTV_rem` ≈ ₹950:

| | Max-recovery policy | Net-value agent |
|---|---|---|
| Contact 1 | 1,000 sent · ~119 recovered · cost ₹7,600 | Same — contact 1 clears the 1.5% bar broadly |
| Contact 2 | 881 sent · ~30 recovered · cost ₹20,924 · gain ~₹21,000 | Sent selectively — roughly break-even, so segment-gated |
| Contact 3 | 810 sent · ~12 recovered · cost ₹53,865 · gain ~₹8,400 | **Sent only to engaged segments** |
| **Contact-3 net** | **−₹45,465** | **≈ 0** |

Max-recovery destroys roughly ₹45k of value to recover ₹8.4k, and it books that ₹8.4k as a success-rate win. That row is the submission.

---

## 6. Intervention five-tuples

**[DESIGN]** throughout. Format: `(flat_cost, annoyance_delta, latency, success_model, hard_preconditions)`.

### 1. `retry_now`
- **Cost** ₹0.60 · **Annoyance** none (no customer contact)
- **Latency** 0h
- **Success** `P̂(recover | belief, immediate, world_time)`
- **Preconditions** `attempts < 6` · `cycle_debits < 3` · `≥4h since last attempt`

### 2. `retry_after(h)`, `h ∈ {4, 12, 24, 48, 72}`
- **Cost** ₹0.60 · **Annoyance** none
- **Latency** exactly `h` hours — world state may change in between, which is the point
- **[P2-CORRECTED]** `h ∈ {24, 36, 48, 72, 120}`. The Phase 0 set `{4, 12, 24, 48, 72}` is
  non-compliant: RBI's 2026 e-mandate framework requires a fresh pre-debit notification at
  least 24h before **every** attempt, so a 4h or 12h retry cannot legally occur.
- **Success** `P̂` evaluated at `t + h`
- **Preconditions** `t + h < expiry` · attempt and cycle caps

### 3. `switch_route_and_retry`
- **Cost** ₹0.90 · **Annoyance** none
- **Latency** 0h
- **Success** `P̂` with the acquirer route flipped
- **Preconditions** `rail == card_mandate` · route switched `< 2` times · caps

### 4. `request_card_update`
- **Cost** ₹0.60 · **Annoyance** `Δchurn_k × LTV_rem` at the current contact index
- **Latency** response delay ~ Exponential, **median 26h**, censored at expiry
- **Success** `P(responds | segment) × 0.94`
  - response probability by segment: **engaged 0.42 · lapsed 0.18 · dormant 0.06**
  - repeat-request decay on the base rate: ×1.00 / ×0.58 / ×0.42 for the 1st / 2nd / 3rd
- **Preconditions** `rail == card_mandate` · `contacts < 3`

### 5. `schedule_retry_at(date)`
- **Cost** ₹0.60 · **Annoyance** none
- **Latency** to the target datetime — this is where the salary-date effect is exploited
- **Success** `P̂` at that date
- **Preconditions** `date < expiry` · caps

### 6. `escalate_to_human`
- **Cost** ₹85.00 · **Annoyance** consumes a contact slot at the current escalation index
- **Latency** queue, median 9h
- **Success** resolves `risk_block` with **p = 0.72**; resolves other causes with p ≈ 0.05
- **Preconditions** `contacts < 3`
- **No minimum-amount precondition, deliberately.** Whether a human should touch a ₹99 recovery is exactly the judgment the value engine exists to make. Hardcoding a floor would steal the decision from the thesis.

### 7. `abandon`
- **Cost** ₹0 · **Annoyance** none · **Latency** terminal · **Success** 0
- **Preconditions** none — always available, and a real competing action in the `argmax`, never a fallback

---

## 7. Hard bounds — outside the economics

### DECISION-012 — The bound layer can only shrink the action set, never expand it

| Bound | Value | Source |
|---|---|---|
| `max_attempts_per_transaction` | 6 | [DESIGN] — a ceiling, not a policy. The agent chooses within it. |
| `max_contacts_per_transaction` | 3 | [DESIGN] |
| `max_debits_per_mandate_cycle` | 3 | **[P2-CORRECTED → chosen]** — asserted here as a network rule. It is not one: India caps no number of retries per cycle. Survives as merchant policy. |
| `network_retry_cap_per_30d` | 10 | **[P2: sourced]** — Mastercard 10/30d binds; Visa allows 15. Added in Phase 2. |
| `min_inter_attempt_hours` | ~~4~~ → **24** | **[P2-CORRECTED → sourced]** — a fresh pre-debit notification is required ≥24h before every attempt, so 4h was non-compliant. |
| `pre_debit_notification_hours` | 24 | **[P2: sourced]** — RBI E-mandate Framework 2026. Added in Phase 2. |
| `afa_threshold_inr` | 15,000 | **[P2: sourced]** — no AFA required below this. Added in Phase 2. |
| `retry_storm_guard` | ≤2 attempts per `(bank_id, hour)` bucket across the whole batch | [DESIGN] — prevents hammering a bank during an outage |
| `card_expired_retry_block` | ≤1 plain retry once `P(card_expired) > 0.50` | [DESIGN] |
| `expiry_horizon` | 21d card / 14d UPI | **[P2: chosen]** — no published force-cancel rule found |

These are deterministic for the same reason the economics are: **a model cannot be the thing that enforces a cap.** Every gate logs when it fires.

---

## 8. The boundary contract

### DECISION-013 — Exactly what the agent may observe

```
transaction_id, merchant_id, customer_id
rail                        # card_mandate | upi_autopay
amount_inr
plan_tenure_months
error_code                  # GW_05 | GW_11 | GW_21 | GW_33 | GW_54 | GW_91
error_message               # free text, noisy, non-canonical
bank_id                     # issuer or payer PSP
card_last4, card_network, card_exp_month, card_exp_year    # null on UPI
mandate_id, mandate_created_at, mandate_debits_this_cycle
attempt_number
prior_attempts[]            # {ts, intervention, error_code, outcome}
customer_history            # {successful_debits_12m, failed_debits_12m,
                            #  last_success_ts, avg_days_late,
                            #  prior_contact_responses, segment_label}
first_failure_ts, expiry_ts, observed_at
```

### Explicitly NOT observable — lives in `world/`, enforced by `tests/test_boundary.py`

```
true_cause · recovery_probability · bank_health · outage_window
will_respond_to_contact · world_state · any world/ symbol whatsoever
```

The agent obtains recovery probabilities **only** from `agent/estimator.py`, fitted on `history.jsonl` (observed outcomes, no causes). It estimates the physics of the world; it never reads them. Without this the agent wins by construction and the result is a tautology.

---

## 9. Exit criterion — a transaction that is recoverable and not worth recovering

**TXN-A7F3** · `card_mandate` · ₹99/month · dormant segment · 14 months tenure
**Hidden ground truth: `card_expired`.** A card update *would* work. This transaction is genuinely recoverable.
**State:** error `GW_21` · one plain retry burned · two card-update requests sent, no response · 9 days left on the horizon.

**Question: send the third contact?**

**Probability of recovery**
| Term | Value |
|---|---|
| `P(cause fixable by card update)` — posterior after two non-responses, shifted toward `mandate_dead` | 0.53 |
| `P(dormant customer responds to a 3rd request)` = 0.06 base × 0.42 decay | 0.0252 |
| `P(new card succeeds \| responds)` | 0.94 |
| **`P(recover)`** | **0.0126 — 1.26%** |

**Value if recovered**
| Term | Value |
|---|---|
| `LTV_remaining` = ₹99 × 4 remaining cycles | ₹396.00 |
| Invoice net of 2% MDR | ₹97.02 |
| Retained LTV = 0.55 × ₹396.00 | ₹217.80 |
| **`V_recover`** | **₹314.82** |

**The arithmetic**
```
Expected gain  = 0.0126 × 314.82               =   ₹3.95
Cost           = ₹0.60 flat + (0.070 × 396.00) =  ₹28.32
                                                 ────────
Net                                              −₹24.37
```

**Decision: ABANDON.**

The required probability at contact 3 is 12.73%. This transaction offers 1.26%. **A max-recovery policy pursues it, occasionally succeeds, and books the success. It is wrong to, by ₹24.37.**

> These figures are computed from `netvalue/world/config.py` and asserted by
> `tests/test_canonical_abandoned_but_recoverable_case`, so this section and the code
> cannot drift apart. Two Phase 0 drafting errors were corrected when the config was
> built: the repeat-response decay was non-monotone (a validator now rejects that), and
> the dormant segment carried two different remaining-cycle figures. Both corrections
> make the example stronger, not weaker — the net moved from −₹29.84 to −₹24.37 and the
> probability gap widened.

Note what makes the example valid: the ground truth is `card_expired`, so this appears in the **abandoned-but-recoverable list** — the artifact that demonstrates business judgment more directly than anything else in the submission.

**Exit criterion satisfied.** Proceed to Phase 1.

---

## 10. Phase 2 resolution

All ten open items were worked. Full sourcing, with grades and URLs, is in
[`CALIBRATION.md`](CALIBRATION.md).

| # | Claim | Outcome |
|---|---|---|
| 1 | BD/TD split ≈ 78/22 | **Kept.** OC-149 confirmed as a real circular (13 May 2022, TD <1%, BD <5%) with a primary source. |
| 2 | UPI Autopay MDR = 0% | **Confirmed.** Currently zero; NPCI permits up to 0.30% P2M. |
| 3 | Card MDR ≈ 2.0% | **Confirmed.** Domestic credit 1.5–2.5%, ~2% headline. |
| 4 | Max 3 debits per mandate cycle | **WRONG.** India caps no retries per cycle. Relabelled merchant policy; sourced network cap of 10/30d added. |
| 5 | Force-cancel at 21d / 14d | **Unsourced.** Relabelled `chosen`. |
| 6 | `p_involuntary_churn` = 0.55 | **Anchored.** 30–50% of involuntary-churn customers reactivate, so 50–70% do not. 0.55 sits inside that band. |
| 7 | `Δchurn` per contact | **Unsourceable.** No public measurement exists — the industry publishes what dunning recovers, never what it costs. Stays `chosen`; defended by the Phase 8 sweep, not by a citation. |
| 8 | Human agent cost ≈ ₹730/hr | **Unsourced.** Relabelled `chosen`. |
| 9 | FY-end decline spike | **Partial.** NPCI attributed a March outage to year-end rush, but no quantified seasonal effect. Stays `chosen`. |
| 10 | `afa_timeout` counted as BD | **Rescoped.** AFA applies only above ₹15,000, and the plan ladder tops out at ₹4,999 — so as written the cause was unreachable. Now covers the pre-debit **opt-out** the 2026 framework attaches to every notification, which applies at any amount. |

Two new sourced parameters entered the config from this work: `network_retry_cap_per_30d`
and `base_failure_rate_by_rail` (UPI Autopay 8–15% against card mandates 2–3% — the rails
must not share a failure profile).

**The most consequential correction is #4/#2.** Together they mean the binding constraint on
retrying is *temporal*, not a count: attempts are rate-limited to one per 24 hours by the
notification requirement. That makes the expiry horizon — not the attempt budget — the thing
that actually binds, which is a more interesting sequential decision problem than the one
Phase 0 specified.

---

## 11. Decisions to record in `DECISIONS.md`

Copy these across at the start of Phase 1, one line each, in your own words — the panel interviews on exactly these:

- **001** Recurring-only world. Why annoyance cost is structural there and decorative in one-shot checkout.
- **002** 31-day March window. Why it must contain both salary dates and the FY-end spike.
- **003** Seven causes, `mandate_dead` as the only permanently unrecoverable class. Why `card_expired` is not in that class.
- **004** Cause priors, and why they were shaped to a BD/TD target rather than chosen freely.
- **005** The `P(code|cause)` matrix. Why `GW_33` sits at 0.687 and not lower.
- **006** The three test thresholds, and why the margins are thin on purpose.
- **007** Plan ladder with 65% of volume at or below ₹149. Why uniform amounts would kill the thesis.
- **008** Recovery value carries retained LTV. Why invoice-only would produce a degenerate abandon-everything agent.
- **009** Annoyance derived as `Δchurn × LTV`, convex in contact index. Why it is a model and not a number.
- **010–011** Attempt costs and horizons.
- **012** Bounds are deterministic and can only shrink the action set. Why a model may not enforce a cap.
- **013** The observable schema, and why `history.jsonl` exists at all.
