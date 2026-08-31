# CALIBRATION.md

**Status: Phase 2 deliverable. Not yet sourced.**

Every value the world uses is listed here. Each row carries a **rail**, a **source**, a
**retrieval date**, and a `sourced | chosen` flag.

Two rules govern this file:

1. **Anything not sourced is flagged `chosen`.** An admitted gap costs nothing. A
   mislabelled source costs everything, because this is precisely the section where the
   reader is being asked to trust the world.
2. **Never cite one rail's data for another rail's failure mode.** NPCI's BD/TD and
   per-bank technical-decline data is **UPI**. `card_expired` and `afa_timeout` on cards
   are **card** failures. Calibrating a card simulator against UPI decline statistics is a
   category error, and it is the kind a payments reviewer spots instantly.

---

## Sourcing plan by rail

| Rail | Where the numbers come from |
|---|---|
| `upi_autopay` | NPCI BD/TD & Uptime page — per-bank technical decline percentages; product-wise declined transactions by issuer with approved / BD / TD split |
| `card_mandate` | Card network and issuer decline data. Do **not** borrow UPI figures here. |
| Both (economics) | E-mandate and subscription dunning recovery + churn research — this also supplies the `Δchurn × LTV` inputs for the annoyance cost |

---

## Open items carried from Phase 0

Each is currently asserted in `netvalue/world/config.py` and flagged `[VERIFY-P2]` in
`PHASE0_DECISIONS.md`. **Any item still unverified when Phase 2 closes is relabelled
`chosen` here, not quietly asserted.**

| # | Claim | Current value | Rail | Risk if wrong | Status |
|---|---|---|---|---|---|
| 1 | BD/TD split | 78 / 22 | UPI | Moderate — reshapes cause priors | ☐ |
| 2 | UPI Autopay MDR | 0.0% | UPI | Low, but it drives rail-differential behaviour | ☐ |
| 3 | Card MDR | 2.0% | Card | Low | ☐ |
| 4 | Max debit attempts per mandate cycle | 3 | Both | **High — asserted as a network rule** | ☐ |
| 5 | Mandate force-cancel horizon | 21d card / 14d UPI | Both | Moderate — sets the DP horizon | ☐ |
| 6 | `p_involuntary_churn` if never recovered | 0.55 | Both | **High — scales every recovery value** | ☐ |
| 7 | `Δchurn` per contact | 0.008 / 0.025 / 0.070 | Both | **Highest — the whole result rests here** | ☐ |
| 8 | Human agent cost, fully loaded | ₹730/hr | — | Low | ☐ |
| 9 | FY-end (31 Mar) decline spike is real | assumed | UPI | Moderate — config B leans on it | ☐ |
| 10 | `afa_timeout` counted as BD | BD | Both | Low — record the caveat either way | ☐ |

### On item 7

This is the parameter the entire submission rests on, and it is also the one the Phase 8
**sensitivity sweep** is designed to neutralise. Rather than defending a single churn
schedule, the sweep reports net value for all four strategies across the full plausible
range of annoyance costs and marks where the policy stops dominating.

Source it as well as the public record allows, then show the reader you did not need them
to believe the specific value.

---

## Structure to build in (Phase 3 consumes this)

- **Per-bank spread is real and large.** Pull the current month rather than reusing
  published historical figures. Different banks must have different failure profiles.
- **Temporal clustering is real.** Financial-year-end closing at banks; salary-date effects
  on insufficient funds. Both must be observable inside the simulation window.
- **Amounts are log-normal with heavy small-ticket mass.** Currently a discrete plan ladder
  with 65% of volume at or below ₹149.

---

## Verification discipline

Verify every circular number and percentage before it enters the repo. A wrong circular
number is worse than no circular number: it converts an honest modelling choice into a
false claim about the regulator.
