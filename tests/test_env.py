"""Credentials load from a local file, and that file never reaches the repository.

The second half is the point. A secret committed to a public repository is not undone by
deleting it — the history keeps it and the key has to be rotated. So the arrangement is
asserted rather than assumed: ``.env`` is ignored, ``.env.example`` is tracked, and the
template holds no real key.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from netvalue.env import KNOWN_KEYS, describe_credentials, load_env, parse

REPO_ROOT = Path(__file__).resolve().parent.parent


def _git_ignores(path: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "-q", path],
        cwd=REPO_ROOT, capture_output=True, check=False,
    )
    return result.returncode == 0


# ------------------------------------------------------------------ the safety property


def test_the_secret_file_is_ignored() -> None:
    """The one that matters. A committed key means rotating it, not reverting a file."""
    assert _git_ignores(".env"), ".env is NOT gitignored — a key committed here is public"


def test_the_template_is_tracked() -> None:
    """Ignoring the template too would leave nobody able to see which keys are needed."""
    assert not _git_ignores(".env.example")
    assert (REPO_ROOT / ".env.example").is_file()


def test_the_secret_file_is_not_in_the_index() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", ".env"],
        cwd=REPO_ROOT, capture_output=True, check=False,
    )
    assert tracked.returncode != 0, ".env is tracked by git"


def test_the_template_holds_no_real_key() -> None:
    """Every documented key must be present and empty. A filled-in template is how a
    secret gets committed by someone who thought they were editing `.env`."""
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    values = parse(text)
    for key in ("GEMINI_API_KEY", "XAI_API_KEY", "ANTHROPIC_API_KEY"):
        assert key in values, f"{key} is undocumented in the template"
        assert values[key] == "", f"{key} has a value in the tracked template"
    # Anything long and secret-shaped, even in a comment.
    assert not re.search(r"\b(sk-[A-Za-z0-9_-]{16,}|AIza[A-Za-z0-9_-]{20,})", text)


def test_every_known_key_is_documented() -> None:
    """A key the loader honours but the template omits is one nobody knows to set."""
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    for key in KNOWN_KEYS:
        assert key in text, f"{key} is loadable but undocumented in .env.example"


# ------------------------------------------------------------------ loader behaviour


def test_parses_the_shapes_people_actually_write() -> None:
    parsed = parse(
        "\n".join(
            [
                "# a comment",
                "",
                "PLAIN=value",
                "  SPACED  =  spaced  ",
                'QUOTED="in quotes"',
                "SINGLE='single'",
                "export EXPORTED=exported",
                "TRAILING=value # trailing comment",
                "EMPTY=",
                "not-a-pair",
            ]
        )
    )
    assert parsed == {
        "PLAIN": "value",
        "SPACED": "spaced",
        "QUOTED": "in quotes",
        "SINGLE": "single",
        "EXPORTED": "exported",
        "TRAILING": "value",
        "EMPTY": "",
    }


def test_the_shell_wins_over_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CI configuration must not be silently replaced by a checked-out file, and one
    command must be overridable without editing anything."""
    env_file = tmp_path / ".env"
    env_file.write_text("NETVALUE_TEST_KEY=from_file\n", encoding="utf-8")
    monkeypatch.setenv("NETVALUE_TEST_KEY", "from_shell")
    load_env(env_file)
    assert os.environ["NETVALUE_TEST_KEY"] == "from_shell"


def test_override_is_available_but_not_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("NETVALUE_TEST_KEY=from_file\n", encoding="utf-8")
    monkeypatch.setenv("NETVALUE_TEST_KEY", "from_shell")
    load_env(env_file, override=True)
    assert os.environ["NETVALUE_TEST_KEY"] == "from_file"


def test_a_blank_value_never_shadows_the_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The template ships every key blank. If a blank line overwrote a real environment
    variable, copying the template would break a working setup."""
    env_file = tmp_path / ".env"
    env_file.write_text("NETVALUE_TEST_KEY=\n", encoding="utf-8")
    monkeypatch.setenv("NETVALUE_TEST_KEY", "from_shell")
    load_env(env_file)
    assert os.environ["NETVALUE_TEST_KEY"] == "from_shell"


def test_a_missing_file_is_not_an_error(tmp_path: Path) -> None:
    """The project runs with no credentials at all, so this cannot raise."""
    assert load_env(tmp_path / "nope.env") == []


def test_load_reports_names_never_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A stray print of the return value must not leak a key into a CI log or a screen
    recording of the demo."""
    env_file = tmp_path / ".env"
    env_file.write_text("NETVALUE_TEST_KEY=super_secret_value\n", encoding="utf-8")
    monkeypatch.delenv("NETVALUE_TEST_KEY", raising=False)
    applied = load_env(env_file)
    assert applied == ["NETVALUE_TEST_KEY"]
    assert "super_secret_value" not in str(applied)


def test_describe_credentials_names_without_revealing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in KNOWN_KEYS:
        monkeypatch.delenv(key, raising=False)
    assert describe_credentials() == "none"
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyEXAMPLEEXAMPLEEXAMPLE")
    described = describe_credentials()
    assert "GEMINI_API_KEY" in described
    assert "AIza" not in described
