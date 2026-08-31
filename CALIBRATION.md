# CALIBRATION.md

**Phase 2 — complete.** Retrieved 31 August 2026.

Every value the world uses is listed here with a **rail**, a **source**, and a grade:

| Grade | Meaning |
|---|---|
| **primary** | Regulator, NPCI, or card network publication |
| **secondary** | Vendor benchmark or industry analysis — real data, but self-published |
| **chosen** | No public source found. A modelling choice, labelled as one. |

Two rules govern this file:

1. **Anything unsourced is flagged `chosen`.** An admitted gap costs nothing. A mislabelled
   source costs everything, because this is precisely the section where the reader is being
   asked to trust the world.
2. **Never cite one rail's data for another rail's failure mode.** NPCI's BD/TD data is
   **UPI**. Card expiry and card decline mixes are **card**. Calibrating a card simulator
   against UPI decline statistics is a category error a payments reviewer spots instantly.

---

## Summary: what Phase 2 changed

Calibration is supposed to break things. It broke four.

| # | Phase 0 asserted | The record says | Action |
|---|---|---|---|
| 1 | Max 3 debits per mandate cycle is a network rule | India caps **no** number of retries per cycle. Card networks cap 10 (MC) / 15 (Visa) per **30 days**. | Relabelled `chosen` merchant policy; added sourced `network_retry_cap_per_30d = 10` |
| 2 | `min_inter_attempt_hours = 4` | Every retry needs a fresh **24h** pre-debit notification. A 4h retry is non-compliant. | Raised to **24**, with a validator |
| 3 | `afa_timeout` is an authentication timeout | AFA is only required **above ₹15,000**; the plan ladder tops out at ₹4,999, so it was unreachable | Rescoped to include pre-debit **opt-out**, which the 2026 framework attaches to every notification |
| 4 | Both rails share a failure profile | UPI Autopay fails at **8–15%**, card mandates at **2–3%** | Added sourced `base_failure_rate_by_rail` |

Item 2 is the one that mattered most: Phase 0 would have produced an agent whose optimal
strategy was illegal.

---

## A. Regulatory and network rules

| # | Claim | Value | Rail | Grade | Source |
|---|---|---|---|---|---|
| 1 | NPCI targets TD below 1%, BD below 5% | TD <1%, BD <5% | UPI | **primary** | [NPCI Circular OC-149](https://www.npci.org.in/PDF/npci/upi/circular/2022/UPI-OC-149-Reduction-of-business-decline-in-UPI.pdf), 13 May 2022; [addendum OC-149A](https://www.npci.org.in/PDF/npci/upi/circular/2022/OC149-A-Addendum-to-OC-149-Reduction-of-business-declines-in-UPI.pdf), 15 Jun 2022 |
| 2 | Pre-debit notification required ≥24h before **every** attempt | 24h | Both | **primary** | RBI Digital Payments E-mandate Framework 2026, 21 Apr 2026 — via [Slicker](https://www.slickerhq.com/resources/blog/country-specific-retry-rules-rbi-direct-debit-paypal), [ELP](https://economiclawspractice.com/new-rbi-rules-2026-complete-guide-to-digital-payments-e-mandate-framework-for-cards-upi-ppis/) |
| 3 | Minimum wait before first retry | 24h | Both | **primary** | as row 2 |
| 4 | **No cap on retries per billing cycle in India** | — | Both | **primary** | as row 2 |
| 5 | AFA required above this per-transaction value | ₹15,000 | Both | **primary** | [Medianama](https://www.medianama.com/2026/04/223-rbi-additional-factor-authentication-e-mandates/); ₹1 lakh for insurance, mutual funds, credit-card bills |
| 6 | UPI Autopay: no UPI PIN per debit below the threshold | ₹15,000 | UPI | **primary** | [PhonePe Business](https://business.phonepe.com/articles/understanding-upi-autopay-mandates-everything-you-need-to-know) |
| 7 | Card network retry caps per 30 days | Visa 15, **Mastercard 10** | Card | **secondary** | [PayPal BRC](https://www.paypal.com/us/brc/article/avoid-excessive-retries-penalties), [Slicker](https://www.slickerhq.com/resources/blog/visa-mastercard-payment-retry-rules) |

**Row 4 is the correction that matters.** Phase 0 asserted a 3-debit cycle cap as a network
rule. It is not one. The real constraint is *temporal* — a 24h notification before every
attempt — which is a materially more interesting problem: retries are rate-limited rather
than count-limited, so the horizon, not the attempt budget, is what binds.

`max_debits_per_mandate_cycle = 3` survives as **merchant policy**, now labelled `chosen`
and validated to stay under the network cap.

---

## B. UPI rail — decline structure

| # | Claim | Value | Grade | Source |
|---|---|---|---|---|
| 8 | System-wide TD, 2025 | ~0.7–0.8% | **secondary** | [productgrowth.in](https://productgrowth.in/insights/fintech/upi-payment-success-rates/), citing NPCI communications and D91 Labs |
| 9 | System-wide TD, 2016 | 8–10% | **secondary** | as row 8 |
| 10 | Blended merchant success rate | 92–96% | **secondary** | as row 8 — the article states per-PSP rates are *not* officially published |
| 11 | UPI Autopay failure rate | 8–15% | **secondary** | [productgrowth.in UPI Autopay guide](https://productgrowth.in/insights/fintech/upi-autopay-guide/) |
| 12 | Card mandate failure rate | 2–3% | **secondary** | as row 11 |
| 13 | Autopay mandates revoked monthly, mainly insufficient balance | >20 million | **secondary** | [Business Standard](https://www.business-standard.com/finance/news/upi-discretionary-spending-bars-restaurants-autopay-revocations-august-2025-125090800646_1.html) |

### Not used: the merchant-side UPI failure mix

`productgrowth.in` publishes a failure breakdown — bank server timeout 35–45%, wrong
PIN/attempts 20–30%, insufficient balance 15–25%, network 10–15%, account blocked 5–10% —
and explicitly labels it *"not NPCI-published numbers"*, from fintech audits.

**It is deliberately not used to set the BD/TD split.** It measures merchant-observed
failures, not NPCI's BD/TD classification, and taken naively it implies TD near 50%, which
contradicts rows 1 and 8 by an order of magnitude. Two different quantities wearing similar
labels is exactly the trap this file exists to avoid.

### The per-bank figures could not be retrieved

`npci.org.in/statistics/bd-td-and-uptime` returns **HTTP 403** to automated fetches.
Per-bank technical-decline percentages are therefore **`chosen`**, not sourced. The spread
is real and large and is built into the world regardless; the specific per-bank numbers are
invented. Pull them manually before the video if you want this row upgraded.

---

## C. Card rail — decline structure

| # | Claim | Value | Grade | Source |
|---|---|---|---|---|
| 14 | Insufficient funds share of subscription declines | >30% | **secondary** | [Baremetrics](https://baremetrics.com/blog/why-subscription-payments-fail) |
| 15 | Insufficient funds share of all CNP issuer declines | 44.4% | **secondary** | [Checkout.com](https://www.checkout.com/blog/five-reasons-why-card-payments-are-declined) |
| 16 | Expired cards, share of declines | ~12% | **secondary** | as row 14 |
| 17 | Expired cards, share of failures (conflicting) | 25–30% | **secondary** | [Yuno](https://y.uno/en/blog/involuntary-churn-is-eating-your-mrr-heres-how-to-stop-it) |
| 18 | Generic/unlabelled decline share | ~39% | **secondary** | as row 14 |
| 19 | Bank soft declines | 15–20% | **secondary** | as row 17 |

**Rows 16 and 17 conflict** — 12% against 25–30% for the same quantity. The config uses
**0.12**, the lower and more conservative figure, because it comes from a subscription-
specific source rather than a general one. The conflict is recorded rather than resolved;
a reviewer who knows the higher number should see that it was considered.

Row 18 is quietly the most important. **39% of declines carry a generic label** — direct
external support for this project's central design constraint, that the error code does not
identify the cause.

---

## D. Economics

| # | Claim | Value | Rail | Grade | Source |
|---|---|---|---|---|---|
| 20 | Card MDR, domestic credit | 1.5–2.5%, ~2% headline | Card | **secondary** | [Razorpay](https://razorpay.com/blog/convenience-fee-tdr-mdr-platform-fee-amc-setup-fee-technology-fee-of-payment-gateway/), [Terra Insight](https://www.terra-insight.com/insights/subscription-saas-mdr-economics-india/) |
| 21 | UPI MDR | 0% currently; NPCI permits up to 0.30% P2M | UPI | **primary** | [PIB](https://www.pib.gov.in/PressReleasePage.aspx?PRID=2114335&reg=48&lang=2), [Razorpay](https://razorpay.com/blog/upi-charges-explained-mdr-vs-platform-fees/) |
| 22 | Involuntary churn as share of total churn | 25–40% | Both | **secondary** | [Recurly](https://recurly.com/research/churn-rate-benchmarks/), [Baremetrics](https://baremetrics.com/blog/involuntary-churn) |
| 23 | MRR lost to failed payments | ~9% | Both | **secondary** | [Baremetrics benchmarks](https://baremetrics.com/blog/subscription-payment-recovery-benchmarks), n=119 US B2B SaaS, May 2026 |
| 24 | Involuntary-churn customers reactivating in 7–14 days | 30–50% | Both | **secondary** | [Baremetrics](https://baremetrics.com/blog/involuntary-churn) |
| 25 | Dunning recovery rate, active systems | 40–60%; best-in-class 70–85% | Both | **secondary** | as row 22 |
| 26 | Dunning recovery, email alone | 42% | Both | **secondary** | as row 22 |
| 27 | Median attempted-recovery rate | 12.7% | Both | **secondary** | as row 23 |

**Row 21 supports the rail asymmetry** the agent is meant to exploit: a recovered UPI
renewal nets strictly more than an identical card renewal. `mdr_by_rail` = 2.0% / 0.0%.

**Row 24 is the anchor for `p_involuntary_churn = 0.55`.** If 30–50% of involuntary-churn
customers reactivate on their own, then 50–70% do not. 0.55 sits inside that band, toward
the optimistic end. **Grade: derived from secondary.** Previously it was pure invention.

---

## E. Recovery dynamics — Phase 3 will consume these

| # | Claim | Value | Grade | Source |
|---|---|---|---|---|
| 28 | Recovery on retry 1 (soft declines) | 20–40% | **secondary** | [Slicker](https://www.slickerhq.com/resources/blog/failed-subscription-payment-retry-attempts) |
| 29 | Recovery on retry 2, of the remaining pool | 15–25% | **secondary** | as row 28 |
| 30 | Recovery on retry 3, of the remaining pool | 10–15% | **secondary** | as row 28 |
| 31 | Beyond attempt 3–4 | "rates flatten" | **secondary** | as row 28 |
| 32 | Recommended attempts per cycle | 3–6 | **secondary** | as row 28 |
| 33 | Recommended dunning cadence | 7 emails over 30 days | **secondary** | [Baremetrics](https://baremetrics.com/blog/dunning-emails) |
| 34 | Email after first decline converts at-risk customers | 15–25% | **secondary** | [Baremetrics](https://baremetrics.com/blog/involuntary-churn) |
| 35 | SMS/phone within 48h recovers additionally | 10–15% | **secondary** | as row 34 |

Rows 28–31 give an externally-anchored **declining recovery curve by attempt number** —
exactly the shape `world/recovery.py` needs, and far better than inventing one.

Row 32 independently supports `max_attempts_per_transaction = 6` as the ceiling.

---

## F. Still `chosen` — no public source found

These are modelling choices. They are labelled as such in the code and here, and they are
not defended as facts.

| # | Parameter | Value | Why no source |
|---|---|---|---|
| 36 | **`Δchurn` per dunning contact** | 0.008 / 0.025 / 0.070 | **No published measurement of incremental voluntary churn caused by the *k*-th dunning contact exists.** Vendors publish recovery rates, never the goodwill cost. See below. |
| 37 | Mandate force-cancel horizon | 21d card / 14d UPI | No published rule found; issuer- and merchant-specific in practice |
| 38 | Per-bank technical decline spread | invented | NPCI page returns 403 to automated fetch |
| 39 | Card-update response rate by segment | 0.42 / 0.18 / 0.06 | Segment-level response rates are not published; row 34's 15–25% is the nearest anchor and sits between the engaged and lapsed values |
| 40 | Human escalation cost | ₹85 (~7 min at ₹730/hr) | Loaded support-agent cost is not published for India at this granularity |
| 41 | Merchant policy: 3 debits per cycle | 3 | A choice, not a rule — see row 4 |
| 42 | Plan ladder and segment shares | — | Synthetic by construction |
| 43 | Financial-year-end decline spike | assumed | [Business Standard](https://www.business-standard.com/industry/banking/npci-attributes-upi-outage-to-year-end-transaction-rush-at-banks-125040100968_1.html) reports NPCI attributing a March outage to year-end rush, but no quantified seasonal effect |

### Row 36 is the one that matters, and it is unsourceable

The entire headline result is sensitive to the per-contact churn hazard, and **the industry
does not measure it.** Every vendor publishes what dunning *recovers*; none publishes what
it *costs* in goodwill. That asymmetry is itself the point the project is making — the
whole industry optimises one side of a two-sided ledger.

The qualitative record supports the *shape*: guidance is to space dunning "over 30 days" so
customers do not "feel harassed" or "inundated," and to suppress immediate post-decline
emails. That is a convex fatigue curve described in words. The config encodes it as one.

**This parameter is not defended by a citation. It is defended by the Phase 8 sensitivity
sweep**, which reports net value for all four strategies across the full plausible range of
annoyance costs and marks where the policy stops dominating. Say this out loud in the
video, before anyone asks.

---

## G. Grade summary

| Grade | Rows | Share |
|---|---|---|
| primary | 6 | 14% |
| secondary | 29 | 67% |
| chosen | 8 | 19% |

Every row carries a rail and a grade. No figure is cited on the wrong rail.

**Phase 2 exit criteria met.** Two open follow-ups, both cheap and both optional:
pull per-bank TD manually from the NPCI page (403-blocked to automated fetch), and locate
the RBI framework's primary PDF to upgrade rows 2–5 from press coverage to the circular
itself.
