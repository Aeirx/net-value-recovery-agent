"""Fit the recovery estimator on the historical log and validate its calibration.

The estimator is the agent's only source of physics, and the value engine multiplies its
probabilities by rupees. If it is not calibrated the value engine is computing fiction, so
this script is the gate: it reports Brier, log-loss and expected calibration error on a
held-out slice of the log, against two baselines that know nothing (the global rate) and
almost nothing (the per-intervention rate).

Writes ``reports/estimator_a.json`` (committed) and a reliability diagram.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from netvalue.agent.calibration import (
    base_rate,
    brier_score,
    expected_calibration_error,
    log_loss,
    reliability,
)
from netvalue.agent.estimator import (
    RecoveryEstimator,
    _read_jsonl,
    select_shrinkage,
    split_by_transaction,
)
from netvalue.agent.features import BACKOFF_LEVELS, from_history_row

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA = REPO_ROOT / "data"
REPORTS = REPO_ROOT / "reports"


def _score(preds: list[float], labels: list[bool]) -> dict[str, float]:
    return {
        "brier": brier_score(preds, labels),
        "log_loss": log_loss(preds, labels),
        "ece": expected_calibration_error(preds, labels),
    }


def plot_reliability(bins: list, path: Path) -> Path | None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:  # pragma: no cover
        return None
    fig, ax = plt.subplots(figsize=(5.6, 5.2))
    ax.plot([0, 1], [0, 1], linestyle="--", color="#999", linewidth=1, label="perfect")
    xs = [b.mean_predicted for b in bins]
    ys = [b.observed_rate for b in bins]
    sizes = [max(20.0, 6.0 * b.count**0.5) for b in bins]
    ax.scatter(xs, ys, s=sizes, color="#2440c4", zorder=3, label="estimator (size = n)")
    ax.plot(xs, ys, color="#2440c4", linewidth=1, alpha=0.6)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_xlabel("predicted P(success)")
    ax.set_ylabel("observed success rate")
    ax.set_title("Reliability — held-out slice of history.jsonl")
    ax.legend(frameon=False, loc="upper left")
    ax.grid(alpha=0.25)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=140)
    plt.close(fig)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", default=str(DATA / "history.jsonl"))
    parser.add_argument("--validation-share", type=float, default=0.25)
    args = parser.parse_args()

    rows = _read_jsonl(args.history)
    train, valid = split_by_transaction(rows, validation_share=args.validation_share)
    labels = [bool(r["succeeded"]) for r in valid]
    print(f"history        {len(rows)} rows · train {len(train)} · validation {len(valid)}")
    print(f"base rate      train {base_rate([bool(r['succeeded']) for r in train]):.3f} · "
          f"validation {base_rate(labels):.3f}")

    # --- shrinkage by validation log-loss ------------------------------------------
    kappa, curve = select_shrinkage(train, valid)
    print("\nshrinkage (pseudo-observations) by validation log-loss:")
    for k, ll in curve.items():
        marker = "  <- chosen" if k == kappa else ""
        print(f"  κ={k:>6.1f}   log-loss {ll:.4f}{marker}")

    est = RecoveryEstimator(kappa).fit(train)
    preds = [est.predict(from_history_row(r)).p for r in valid]

    # --- baselines that know nothing / almost nothing ------------------------------
    global_p = base_rate([bool(r["succeeded"]) for r in train])
    by_intervention: dict[str, list[bool]] = {}
    for r in train:
        by_intervention.setdefault(str(r["intervention"]), []).append(bool(r["succeeded"]))
    per_int = {k: base_rate(v) for k, v in by_intervention.items()}
    preds_global = [global_p] * len(valid)
    preds_perint = [per_int.get(str(r["intervention"]), global_p) for r in valid]

    scores = {
        "global_rate": _score(preds_global, labels),
        "per_intervention": _score(preds_perint, labels),
        "estimator": _score(preds, labels),
    }
    print("\n| model | Brier ↓ | log-loss ↓ | ECE ↓ |")
    print("|---|---:|---:|---:|")
    for name, s in scores.items():
        print(f"| `{name}` | {s['brier']:.4f} | {s['log_loss']:.4f} | {s['ece']:.4f} |")

    bins = reliability(preds, labels)
    print("\nreliability (held-out):")
    print(f"  {'bin':<12}{'n':>6}{'predicted':>11}{'observed':>10}{'gap':>8}")
    for b in bins:
        print(f"  [{b.low:.1f}, {b.high:.1f}){b.count:>6}{b.mean_predicted:>11.3f}"
              f"{b.observed_rate:>10.3f}{b.gap:>+8.3f}")

    # --- backoff behaviour -----------------------------------------------------------
    depths = [est.predict(from_history_row(r)).backoff_depth for r in valid]
    depth_hist = {d: depths.count(d) for d in sorted(set(depths))}
    print("\nfinest level with support, validation rows (0 = full 9-feature cell):")
    for d, n in depth_hist.items():
        print(f"  level {d} ({len(BACKOFF_LEVELS[d])} features): {n:>5} rows")

    # --- persist --------------------------------------------------------------------
    REPORTS.mkdir(exist_ok=True)
    image = plot_reliability(bins, REPORTS / "estimator_reliability_a.png")
    summary = {
        "history_rows": len(rows),
        "train_rows": len(train),
        "validation_rows": len(valid),
        "validation_share": args.validation_share,
        "shrinkage": kappa,
        "shrinkage_curve": {str(k): round(v, 5) for k, v in curve.items()},
        "scores": {k: {m: round(v, 5) for m, v in s.items()} for k, s in scores.items()},
        "reliability": [
            {
                "low": b.low, "high": b.high, "n": b.count,
                "predicted": round(b.mean_predicted, 4),
                "observed": round(b.observed_rate, 4),
            }
            for b in bins
        ],
        "backoff_depth_histogram": {str(k): v for k, v in depth_hist.items()},
        "estimator": est.describe(),
    }
    out = REPORTS / "estimator_a.json"
    out.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"\nwrote {out}" + (f" and {image}" if image else ""))

    # The gate. An uncalibrated estimator must not pass silently.
    e, g = scores["estimator"], scores["global_rate"]
    if e["brier"] >= g["brier"]:
        print("\nFAILED: the estimator does not beat the global rate on Brier score")
        return 1
    if e["ece"] > 0.05:
        print(f"\nFAILED: ECE {e['ece']:.4f} exceeds 0.05 — not calibrated enough to price")
        return 1
    print("\nPASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
