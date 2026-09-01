"""On-disk response cache, keyed on the exact request.

Two problems, one mechanism.

**Reproducibility.** The datasets are frozen, but a model is not a pure function. Without
a cache, re-running the evaluation produces different diagnoses, different decisions and
different numbers — so the audit log would not reproduce, the README figures would drift,
and "frozen experiment" would be a claim about the inputs only.

**Cost.** A full diagnosis pass over the evaluation set costs real money. It should cost
it *once*. Every later run — every test, every CI job, every rehearsal of the demo, every
re-run after an unrelated bug fix — replays from disk for nothing.

The key is ``sha256`` over the model id, every request parameter, and the full prompt. Any
change to any of those is a different question and correctly misses the cache. That makes
a stale hit impossible rather than unlikely.

The cache file is committed. It is the difference between a demo that depends on an API
being healthy at the moment you press play and one that does not.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS responses (
    key           TEXT PRIMARY KEY,
    model         TEXT NOT NULL,
    payload       TEXT NOT NULL,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cached_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS responses_model ON responses(model);
"""


@dataclass(frozen=True, slots=True)
class CachedResponse:
    payload: dict[str, Any]
    input_tokens: int
    output_tokens: int


def request_key(*, model: str, prompt: str, params: dict[str, Any]) -> str:
    """A stable digest of everything that could change the answer.

    ``params`` is serialised with sorted keys so that a dict built in a different order is
    still the same request — otherwise the cache would miss for no reason and quietly
    charge for it.
    """
    blob = json.dumps(
        {"model": model, "prompt": prompt, "params": params},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class ResponseCache:
    """SQLite-backed. Concurrent readers are fine; writes are serialised."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.hits = 0
        self.misses = 0
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30.0)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def get(self, key: str) -> CachedResponse | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload, input_tokens, output_tokens FROM responses WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            self.misses += 1
            return None
        self.hits += 1
        return CachedResponse(json.loads(row[0]), int(row[1]), int(row[2]))

    def put(
        self,
        key: str,
        *,
        model: str,
        payload: dict[str, Any],
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO responses "
                "(key, model, payload, input_tokens, output_tokens, cached_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    key,
                    model,
                    json.dumps(payload, sort_keys=True),
                    input_tokens,
                    output_tokens,
                    time.time(),
                ),
            )

    def __len__(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0])

    def stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(input_tokens), 0), "
                "COALESCE(SUM(output_tokens), 0) FROM responses"
            ).fetchone()
        return {
            "entries": int(row[0]),
            "input_tokens": int(row[1]),
            "output_tokens": int(row[2]),
            "hits": self.hits,
            "misses": self.misses,
        }
