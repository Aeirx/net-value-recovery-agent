"""Score every diagnosis arm on the frozen dataset.

Answers the question the ablation needs: how much better than a good rule table is the
model, and how much headroom is left to a perfect diagnoser — measured in **rupees of
regret**, not accuracy, because the confusions are wildly unequal in cost.

**Nothing here calls the API unless you pass --live.** The rules and oracle arms are free
and deterministic. The LLM arm replays from ``data/llm_cache.sqlite``; on a cache miss in
offline mode it fails loudly rather than quietly spending money. Use ``--estimate-cost``
to see what a live run would cost before authorising one.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from netvalue.agent.diagnose import evidence
from netvalue.agent.diagnose.llm import SYSTEM_PROMPT, LLMDiagnoser
from netvalue.agent.diagnose.oracle import OracleDiagnoser, truth_map
from netvalue.agent.diagnose.rules import RulesDiagnoser
from netvalue.agent.diagnose.schema import CausePosterior
from netvalue.eval.diagnosis import (
    confidence_reliability,
    evaluate,
    format_confusion,
    regret_matrix,
)
from netvalue.eval.runner import to_observation
from netvalue.llm.client import (
    PRICING_PER_MTOK,
    OfflineCacheMiss,
    Provider,
    StructuredClient,
    infer_provider,
)
from netvalue.world.config import CONFIG_A, CONFIG_B, Cause
from netvalue.world.generator import load_transactions

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA = REPO_ROOT / "data"
REPORTS = REPO_ROOT / "reports"

#: Rough tokens-per-character for English prose. Only used for the cost estimate.
_CHARS_PER_TOKEN = 3.7


def estimate_cost(observations: list, model: str) -> dict[str, float]:
    """What a live run would cost, before you authorise one."""
    system_tokens = len(SYSTEM_PROMPT) / _CHARS_PER_TOKEN
    view_tokens = [len(evidence.build(o)) / _CHARS_PER_TOKEN for o in observations]
    n = len(observations)
    # The system prompt is byte-identical every call and is cached, so it is paid at full
    # rate once and at roughly a tenth thereafter.
    input_tokens = system_tokens + 0.1 * system_tokens * (n - 1) + sum(view_tokens)
    output_tokens = n * 260.0  # seven probabilities plus a sentence or two
    rate_in, rate_out = PRICING_PER_MTOK.get(model, (0.0, 0.0))
    return {
        "calls": float(n),
        "input_tokens": round(input_tokens),
        "output_tokens": round(output_tokens),
        "usd": round(
            input_tokens / 1e6 * rate_in + output_tokens / 1e6 * rate_out, 2
        ),
    }


def list_models(model: str) -> int:
    """Ask the provider what this key can reach. Model names drift and guessing wastes a
    run; every OpenAI-compatible endpoint answers GET /models."""
    import openai

    client = StructuredClient(model=model, cache_path=DATA / "llm_cache.sqlite")
    if client.provider is Provider.ANTHROPIC:
        print("Use `ant models list` or the Anthropic console for this provider.")
        return 0
    if not client.has_credentials():
        print(f"{StructuredClient.credential_env_var(client.provider)} is not set.")
        return 1

    import os

    from netvalue.llm.client import GEMINI_BASE_URL, LOCAL_BASE_URL, XAI_BASE_URL

    match client.provider:
        case Provider.GEMINI:
            base, key = GEMINI_BASE_URL, (
                os.environ.get("GEMINI_API_KEY") or os.environ["GOOGLE_API_KEY"]
            )
        case Provider.LOCAL:
            base = os.environ.get("LOCAL_LLM_BASE_URL", LOCAL_BASE_URL)
            key = "not-needed"
        case _:
            base, key = XAI_BASE_URL, os.environ["XAI_API_KEY"]

    try:
        for m in openai.OpenAI(api_key=key, base_url=base).models.list():
            print(m.id)
    except Exception as exc:
        print(f"could not list models: {exc}")
        return 1
    return 0


def run_llm_arm(
    diagnoser: LLMDiagnoser,
    observations: list,
    client: StructuredClient,
    max_live_calls: int,
) -> list[CausePosterior]:
    """Diagnose as far as the quota allows, keeping everything completed.

    A free tier has a daily cap, so a full pass may not fit in one day. Every successful
    response is already written to the cache before this returns, so a later run resumes
    from here rather than starting over — which is the whole reason the cache is keyed on
    the request rather than on a run id.
    """
    out: list[CausePosterior] = []
    for i, obs in enumerate(observations, start=1):
        if max_live_calls and client.usage.live_calls >= max_live_calls:
            print(f"  stopped at {i - 1}/{len(observations)}: hit --max-live-calls")
            break
        try:
            out.append(diagnoser.diagnose(obs))
        except Exception as exc:
            print(f"  stopped at {i - 1}/{len(observations)}: {type(exc).__name__}: {exc}")
            print("  everything completed so far is cached; re-run to resume.")
            break
        if i % 50 == 0:
            print(f"  {i}/{len(observations)} "
                  f"({client.usage.live_calls} live, {client.usage.cached_calls} cached)")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", choices=["a", "b"], default="a")
    parser.add_argument("--limit", type=int, default=0, help="0 = the whole dataset")
    parser.add_argument(
        "--model", default="claude-opus-5",
        help="claude-* uses ANTHROPIC_API_KEY; grok-* uses XAI_API_KEY",
    )
    parser.add_argument(
        "--live", action="store_true",
        help="allow real API calls on a cache miss. Costs money. Off by default.",
    )
    parser.add_argument("--estimate-cost", action="store_true")
    parser.add_argument(
        "--max-live-calls", type=int, default=0,
        help="stop after this many uncached calls (0 = no limit). Free tiers have daily "
             "caps; cached work is kept, so a later run resumes where this one stopped.",
    )
    parser.add_argument(
        "--list-models", action="store_true",
        help="ask the provider which models this key can use, then exit",
    )
    args = parser.parse_args()

    if args.list_models:
        return list_models(args.model)

    cfg = CONFIG_A if args.config == "a" else CONFIG_B
    dataset = DATA / f"dataset_{args.config}.jsonl"
    if not dataset.exists():
        print(f"missing {dataset}; run scripts/generate_datasets.py first")
        return 1

    txns = load_transactions(dataset)
    if args.limit:
        txns = txns[: args.limit]
    observations = [
        to_observation(t, now=t.first_failure_at, attempt_number=1, contacts_used=0, prior=())
        for t in txns
    ]
    truths = [t.true_cause.value for t in txns]

    print(f"# Diagnosis — config {args.config.upper()}")
    print(f"\n{len(txns)} transactions · config `{cfg.config_hash()[:16]}`\n")

    if args.estimate_cost:
        est = estimate_cost(observations, args.model)
        provider = infer_provider(args.model)
        if provider is Provider.LOCAL:
            print(f"{args.model} runs on your own machine: no key, no bill, no network.")
            print(f"{int(est['calls'])} calls, ~{int(est['input_tokens']):,} input + "
                  f"{int(est['output_tokens']):,} output tokens. Cost: $0.00.")
            print("Budget roughly 30-60 minutes for a 7-8B model on a laptop GPU; it is")
            print("cached afterwards, so you pay the wall-clock once.")
            return 0
        if args.model not in PRICING_PER_MTOK:
            print(f"! No cached pricing for {args.model}; the estimate below is zero.")
        print(f"A live run on {args.model} ({provider.value}) would make "
              f"{int(est['calls'])} calls, "
              f"~{int(est['input_tokens']):,} input + {int(est['output_tokens']):,} output "
              f"tokens, costing about **${est['usd']}**.")
        print("Cached afterwards, so it is paid once. Re-runs are free.")
        return 0

    # --- the regret matrix, before any diagnoser runs -----------------------------
    matrix = regret_matrix(cfg)
    pairs = sorted(
        ((t, d, v) for (t, d), v in matrix.items() if t is not d),
        key=lambda x: x[2], reverse=True,
    )
    print("Most expensive confusions, Rs of net value destroyed per transaction:\n")
    print("| True cause | Diagnosed as | Regret ₹ |")
    print("|---|---|---:|")
    for t, d, v in pairs[:6]:
        print(f"| `{t.value}` | `{d.value}` | {v:,.0f} |")
    cheap = [p for p in pairs if p[2] < 5.0]
    print(f"\n{len(cheap)} of {len(pairs)} confusions cost under ₹5; the top one costs "
          f"₹{pairs[0][2]:,.0f}. An accuracy figure scores those identically.")

    # --- the arms -------------------------------------------------------------------
    arms: list[tuple[str, object]] = [
        ("rules", RulesDiagnoser()),
        ("oracle", OracleDiagnoser(truth_map([t.model_dump(mode="json") for t in txns]))),
    ]

    client = StructuredClient(
        model=args.model,
        cache_path=DATA / "llm_cache.sqlite",
        offline=not args.live,
    )
    llm_available = len(client.cache) > 0 or (args.live and client.has_credentials())
    if llm_available:
        if args.live:
            # Say what this is about to cost before it costs it. Cached entries are free,
            # so only the uncached remainder is quoted.
            est = estimate_cost(observations, args.model)
            print()
            if client.provider is Provider.LOCAL:
                print(f"--live on {args.model}: up to {int(est['calls'])} local calls, "
                      f"$0.00 ({len(client.cache)} already in the cache).")
            else:
                print(f"--live on {args.model}: up to {int(est['calls'])} calls, "
                      f"about ${est['usd']} if none are cached "
                      f"({len(client.cache)} already in the cache).")
        arms.insert(1, ("llm", LLMDiagnoser(client)))
    else:
        reason = (
            f"--live was passed but "
            f"{StructuredClient.credential_env_var(infer_provider(args.model))} is not set"
            if args.live
            else "cache is empty and --live was not passed"
        )
        print(f"\n! LLM arm skipped: {reason}.")
        print("! Run with --estimate-cost to price it, then --live to populate the cache.")

    reports = []
    for name, diagnoser in arms:
        try:
            posteriors: list[CausePosterior] = [
                diagnoser.diagnose(o) for o in observations  # type: ignore[attr-defined]
            ]
        except OfflineCacheMiss as exc:
            print(f"\n! {name} arm incomplete: {exc}")
            continue
        reports.append(evaluate(cfg, name, posteriors, truths))
        if name == "llm":
            print(f"\nLLM usage: {json.dumps(client.usage.summary())}")

    print("\n| Diagnoser | Accuracy | Top-2 | Mean regret ₹ | Total regret ₹ | "
          "Confidence | Entropy bits | Confidence ECE |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|")
    for r in reports:
        print(f"| `{r.diagnoser}` | {r.accuracy:.1%} | {r.top2_accuracy:.1%} | "
              f"{r.mean_regret_inr:,.1f} | {r.total_regret_inr:,.0f} | "
              f"{r.mean_confidence:.2f} | {r.mean_entropy_bits:.2f} | {r.ece:.3f} |")

    for r in reports:
        if r.diagnoser == "oracle":
            continue
        print(f"\n### `{r.diagnoser}` — worst confusions by total cost\n")
        print("| True | Diagnosed | n | Total regret ₹ |")
        print("|---|---|---:|---:|")
        for t, d, n, total in r.worst_confusions:
            print(f"| `{t}` | `{d}` | {n} | {total:,.0f} |")

    main_arm = next((r for r in reports if r.diagnoser != "oracle"), None)
    if main_arm:
        print(f"\n### `{main_arm.diagnoser}` confusion matrix (rows = truth)\n```")
        print(format_confusion(main_arm))
        print("```")

        idx = [r.diagnoser for r in reports].index(main_arm.diagnoser)
        posteriors_main = [
            arms[idx][1].diagnose(o) for o in observations  # type: ignore[attr-defined]
        ]
        print("\nConfidence calibration — when it says X%, is it right X% of the time?\n")
        print("| Confidence bin | n | Stated | Actual | Gap |")
        print("|---|---:|---:|---:|---:|")
        for b in confidence_reliability(posteriors_main, truths):
            print(f"| [{b.low:.1f}, {b.high:.1f}) | {b.count} | {b.mean_predicted:.2f} | "
                  f"{b.observed_rate:.2f} | {b.gap:+.2f} |")

    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / f"diagnosis_{args.config}.json"
    out.write_text(
        json.dumps(
            {
                "config_hash": cfg.config_hash(),
                "n": len(txns),
                "model": args.model if llm_available else None,
                "regret_matrix": {
                    f"{t.value}->{d.value}": round(v, 2)
                    for (t, d), v in matrix.items()
                    if t is not d
                },
                "arms": [r.summary() for r in reports],
                "confusion": {r.diagnoser: r.confusion for r in reports},
                "llm_usage": client.usage.summary() if llm_available else None,
            },
            indent=2, sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"\nwrote {out}")

    rules = next((r for r in reports if r.diagnoser == "rules"), None)
    if rules is None:
        return 1
    n_causes = len(Cause)
    if rules.accuracy <= 1.0 / n_causes:
        print("\nFAILED: the rules arm is no better than guessing")
        return 1
    print("\nPASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
