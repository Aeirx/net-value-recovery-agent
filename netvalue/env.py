"""Load API credentials from a local ``.env`` file.

Twenty lines rather than a dependency, because the semantics matter more than the parsing
and they are worth being explicit about:

**The real environment always wins.** A value already exported in the shell is never
overwritten by the file. That ordering is what makes CI safe — a build machine's
configuration cannot be silently replaced by a checked-out file — and it lets you override
a single key for one command without editing anything.

**A missing file is not an error.** Every entry point calls this unconditionally; the
project runs perfectly well with no credentials at all (the rules and oracle arms need
none, and a cached LLM arm replays offline).

The file itself is gitignored. ``.env.example`` is the tracked template, and
``tests/test_env.py`` asserts that arrangement cannot silently reverse.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_PATH = REPO_ROOT / ".env"

#: The credentials this project knows how to use. Listed so the loader can report what it
#: found without ever printing a value.
KNOWN_KEYS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "XAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "LOCAL_LLM_BASE_URL",
    "LOCAL_LLM_API_KEY",
)


def parse(text: str) -> dict[str, str]:
    """Parse ``KEY=value`` lines, tolerating comments, blanks, quotes and ``export``."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().split(" #", 1)[0].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def load_env(path: str | Path | None = None, *, override: bool = False) -> list[str]:
    """Populate ``os.environ`` from the file. Returns the names that were set.

    Values are never returned or logged — only names — so a stray debug print cannot leak
    a key into a terminal, a CI log, or a screen recording of the demo.
    """
    env_path = Path(path) if path is not None else DEFAULT_ENV_PATH
    if not env_path.is_file():
        return []

    applied: list[str] = []
    for key, value in parse(env_path.read_text(encoding="utf-8")).items():
        if not value:
            continue
        if key in os.environ and not override:
            continue  # the shell wins
        os.environ[key] = value
        applied.append(key)
    return applied


def describe_credentials() -> str:
    """Which credentials are visible, without revealing any of them."""
    present = [k for k in KNOWN_KEYS if os.environ.get(k)]
    return ", ".join(present) if present else "none"
