"""Generate and freeze the three datasets.

Run once. After the freeze the datasets are never regenerated: if they move because a
score disappointed you, the final number means nothing and — worse — you will not be able
to tell that it means nothing.

Writes ``data/manifest.json`` recording the content hash of every file, both config
hashes, every seed and the git SHA, so any reported number traces to the exact world that
produced it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from netvalue.world.banks import build_world_health
from netvalue.world.config import CONFIG_A, CONFIG_B, Cause, ErrorCode, WorldConfig
from netvalue.world.generator import Transaction, generate_transactions, write_jsonl
from netvalue.world.history import simulate_history
from netvalue.world.recovery import RecoveryContext, is_ever_recoverable

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA = REPO_ROOT / "data"


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        )
        return out.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def net_negative_share(cfg: WorldConfig, txns: list[Transaction]) -> dict[str, float]:
    """Fraction of the population that is recoverable but not worth recovering.

    Reported as a **property of the dataset**, computed before any agent runs. If this is
    near zero the annoyance cost is not biting and the submission has no thesis; if it is
    near one the agent will simply abandon everything.

    The test applied is the contact-3 crossover from the cost model: a transaction counts
    as net-negative when the value of a further customer contact does not clear the bar,
    even granting a correct diagnosis.
    """
    health = build_world_health(cfg)
    bar = cfg.required_recovery_prob_asymptotic(3)
    recoverable = 0
    net_negative = 0

    for txn in txns:
        ctx = RecoveryContext(
            transaction_id=txn.transaction_id, true_cause=txn.true_cause, rail=txn.rail,
            amount_inr=txn.amount_inr, segment=txn.segment, bank_id=txn.bank_id,
            route=txn.acquirer_route, when=txn.first_failure_at, attempt_index=1,
            contact_index=3, health=health,
        )
        if not is_ever_recoverable(cfg, ctx):
            continue
        recoverable += 1

        base = cfg.segments[txn.segment].contact_response_prob
        p_responds = cfg.costs.card_update.response_prob(base, 3)
        p_recover = p_responds * cfg.costs.card_update.p_success_given_response
        if p_recover < bar:
            net_negative += 1

    total = len(txns)
    return {
        "recoverable": recoverable / total,
        "recoverable_but_net_negative_at_contact_3": net_negative / total,
    }


def summarise(cfg: WorldConfig, txns: list[Transaction]) -> dict[str, object]:
    causes = Counter(t.true_cause for t in txns)
    codes = Counter(t.error_code for t in txns)
    rails = Counter(t.rail for t in txns)
    bd = sum(
        n for c, n in causes.items() if cfg.causes[c].decline_class.value == "business_decline"
    )
    return {
        "n": len(txns),
        "bd_share": round(bd / len(txns), 4),
        "rail_share": {r.value: round(n / len(txns), 4) for r, n in rails.items()},
        "cause_share": {
            c.value: round(causes.get(c, 0) / len(txns), 4) for c in Cause
        },
        "code_share": {
            c.value: round(codes.get(c, 0) / len(txns), 4) for c in ErrorCode if codes.get(c)
        },
        "economics": {
            k: round(v, 4) for k, v in net_negative_share(cfg, txns).items()
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force", action="store_true",
        help="regenerate even though a manifest already exists (the freeze guard)",
    )
    args = parser.parse_args()

    manifest_path = DATA / "manifest.json"
    if manifest_path.exists() and not args.force:
        print("REFUSING: data/manifest.json exists — the datasets are frozen.")
        print()
        print("Regenerating after the freeze invalidates every number already reported,")
        print("and the damage is silent. If you genuinely mean to re-freeze, pass --force")
        print("and record why in DECISIONS.md.")
        return 1

    written: dict[str, str] = {}

    for cfg, filename in ((CONFIG_A, "dataset_a.jsonl"), (CONFIG_B, "dataset_b.jsonl")):
        txns = generate_transactions(cfg)
        path = DATA / filename
        write_jsonl(path, txns, include_ground_truth=True)
        written[filename] = file_hash(path)
        cfg.write_json(DATA / f"config_{cfg.name.split('_')[-1]}.json")
        print(f"{filename}: {len(txns)} transactions")
        print(json.dumps(summarise(cfg, txns), indent=2))
        print()

    health = build_world_health(CONFIG_A)
    records = simulate_history(CONFIG_A, health)
    hist_path = DATA / "history.jsonl"
    with hist_path.open("w", encoding="utf-8", newline="\n") as fh:
        for r in records:
            fh.write(json.dumps(r.model_dump(mode="json"), sort_keys=True) + "\n")
    written["history.jsonl"] = file_hash(hist_path)

    succ = sum(1 for r in records if r.succeeded)
    print(f"history.jsonl: {len(records)} logged actions, {succ} succeeded "
          f"({succ / len(records):.1%})")

    manifest = {
        "frozen_at": datetime.now(UTC).isoformat(),
        "git_sha": git_sha(),
        "files": written,
        "configs": {
            "config_a": {"hash": CONFIG_A.config_hash(), "seed": CONFIG_A.seed},
            "config_b": {"hash": CONFIG_B.config_hash(), "seed": CONFIG_B.seed},
        },
        "regime_b": CONFIG_B.regime.model_dump(mode="json"),
    }
    with manifest_path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    print(f"\nfrozen. manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
