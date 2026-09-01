"""End-to-end reproducibility: regenerating the world must reproduce the frozen bytes.

This is the strongest form of the freeze guarantee. ``test_world.py`` checks that the
files on disk still hash to what the manifest recorded; this checks that *running the
generator again from the committed code* produces those same bytes.

Without it, "frozen" would only mean "nobody has edited the file", not "this world is
recoverable from source" — and the second is what makes a reported number traceable.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from netvalue.world.banks import build_world_health
from netvalue.world.config import CONFIG_A, CONFIG_B, WorldConfig
from netvalue.world.generator import generate_transactions, write_jsonl
from netvalue.world.history import simulate_history

DATA = Path(__file__).resolve().parent.parent / "data"


def _manifest() -> dict:
    path = DATA / "manifest.json"
    if not path.exists():
        pytest.skip("datasets not frozen; run scripts/generate_datasets.py")
    return json.loads(path.read_text(encoding="utf-8"))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    ("cfg", "filename"),
    [(CONFIG_A, "dataset_a.jsonl"), (CONFIG_B, "dataset_b.jsonl")],
    ids=["a", "b"],
)
def test_dataset_regenerates_byte_identically(
    cfg: WorldConfig, filename: str, tmp_path: Path
) -> None:
    manifest = _manifest()
    out = tmp_path / filename
    write_jsonl(out, generate_transactions(cfg), include_ground_truth=True)
    assert _sha(out) == manifest["files"][filename], (
        f"{filename} no longer regenerates to its frozen bytes. Either the world code "
        f"changed (in which case re-freeze deliberately and record it in DECISIONS.md) "
        f"or determinism has broken, which invalidates every paired comparison."
    )


def test_history_regenerates_byte_identically(tmp_path: Path) -> None:
    manifest = _manifest()
    records = simulate_history(CONFIG_A, build_world_health(CONFIG_A))
    out = tmp_path / "history.jsonl"
    with out.open("w", encoding="utf-8", newline="\n") as fh:
        for r in records:
            fh.write(json.dumps(r.model_dump(mode="json"), sort_keys=True) + "\n")
    assert _sha(out) == manifest["files"]["history.jsonl"]


def test_config_hashes_are_reproducible() -> None:
    manifest = _manifest()
    assert CONFIG_A.config_hash() == manifest["configs"]["config_a"]["hash"]
    assert CONFIG_B.config_hash() == manifest["configs"]["config_b"]["hash"]


def test_keyed_draws_are_independent_of_call_order() -> None:
    """The property the Phase 4 paired comparison rests on.

    A policy that takes a different path must not shift the world's latent randomness.
    Asking for attempt 3's draw first must give the same answer as asking for it last.
    """
    from netvalue.world import rng

    forward = [rng.bernoulli(7, 0.5, "debit", "TXN-1", k) for k in (1, 2, 3)]
    backward = [rng.bernoulli(7, 0.5, "debit", "TXN-1", k) for k in (3, 2, 1)][::-1]
    assert forward == backward

    # And a different transaction must not share them.
    other = [rng.bernoulli(7, 0.5, "debit", "TXN-2", k) for k in (1, 2, 3)]
    assert forward != other or len(set(forward)) == 1
