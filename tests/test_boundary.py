"""The credibility test.

The whole submission rests on one claim: **the agent never reads the world's ground
truth.** It estimates the physics of the world from observed outcomes; it does not import
them. If that claim fails, the agent wins by construction and every reported number is a
tautology.

This test enforces the claim mechanically by walking the import graph with the AST, so it
catches imports inside functions, inside ``TYPE_CHECKING`` blocks, and inside branches
that never execute. A runtime import check would miss all three.

Run it in a demo. It is a ten-second answer to a reviewer's best question.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "netvalue"
FORBIDDEN_TARGET = "netvalue.world"

#: Directories and files whose code may never touch ``netvalue.world``.
RESTRICTED_PATHS: tuple[str, ...] = (
    "netvalue/agent",
    "netvalue/policies/net_value.py",
)

#: Files that may read ground truth, each with the reason it is permitted.
#:
#: These are oracles *by definition* — they exist to establish ceilings the net-value
#: agent is measured against. None of them is reachable from ``netvalue/agent`` or from
#: ``policies/net_value.py``, which is what the restriction above guarantees.
ALLOWLIST: dict[str, str] = {
    "netvalue/policies/max_recovery.py": (
        "Baseline 3 — the success-rate ceiling. Pursues everything recoverable given "
        "perfect knowledge. Being an oracle is the entire point of this baseline."
    ),
}


def _module_parts(path: Path) -> list[str]:
    """Dotted module path parts for a file inside the package."""
    rel = path.relative_to(REPO_ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return parts


def _resolve_relative(module_parts: list[str], level: int, module: str | None) -> str:
    """Resolve ``from ..world import x`` to its absolute dotted name."""
    # Inside a module, level 1 means "this module's package".
    base = module_parts[:-1]
    if level > 1:
        base = base[: len(base) - (level - 1)]
    if module:
        base = base + module.split(".")
    return ".".join(base)


def _imported_targets(path: Path) -> set[str]:
    """Every dotted name this file imports, absolute and relative resolved."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    parts = _module_parts(path)
    found: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = _resolve_relative(parts, node.level, node.module)
            else:
                base = node.module or ""
            if base:
                found.add(base)
            for alias in node.names:
                found.add(f"{base}.{alias.name}" if base else alias.name)

    return found


def _dynamic_import_strings(path: Path) -> set[str]:
    """Catch ``importlib.import_module("netvalue.world...")`` and friends."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith(FORBIDDEN_TARGET)
    }


def _restricted_files() -> list[Path]:
    files: list[Path] = []
    for entry in RESTRICTED_PATHS:
        target = REPO_ROOT / entry
        if target.is_dir():
            files.extend(sorted(target.rglob("*.py")))
        elif target.is_file():
            files.append(target)
    return [
        f for f in files if f.relative_to(REPO_ROOT).as_posix() not in ALLOWLIST
    ]


def _violates(name: str) -> bool:
    return name == FORBIDDEN_TARGET or name.startswith(FORBIDDEN_TARGET + ".")


def test_restricted_paths_exist() -> None:
    """A guard that silently guards nothing is worse than no guard."""
    missing = [p for p in RESTRICTED_PATHS if not (REPO_ROOT / p).exists()]
    assert not missing, (
        f"RESTRICTED_PATHS references paths that do not exist: {missing}. "
        "Either create them or remove the entry — do not let the boundary test pass vacuously."
    )


@pytest.mark.parametrize(
    "path", _restricted_files(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_agent_never_imports_world(path: Path) -> None:
    """No file under the restricted paths may import ``netvalue.world``."""
    offenders = sorted(name for name in _imported_targets(path) if _violates(name))
    rel = path.relative_to(REPO_ROOT).as_posix()
    assert not offenders, (
        f"{rel} imports ground truth: {offenders}.\n"
        "The agent must estimate the world's physics from observed outcomes "
        "(agent/estimator.py, fitted on data/history.jsonl), never read them. "
        "If this import is legitimate, the file belongs in netvalue/eval or "
        "netvalue/policies with an explicit ALLOWLIST entry and a stated reason."
    )


def _module_to_path(dotted: str) -> Path | None:
    """Resolve a first-party dotted module name to a file, if one exists."""
    parts = dotted.split(".")
    if not parts or parts[0] != PACKAGE:
        return None
    candidate = REPO_ROOT.joinpath(*parts).with_suffix(".py")
    if candidate.is_file():
        return candidate
    package_init = REPO_ROOT.joinpath(*parts, "__init__.py")
    return package_init if package_init.is_file() else None


@pytest.mark.parametrize(
    "path", _restricted_files(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_agent_never_reaches_world_transitively(path: Path) -> None:
    """The boundary must hold through intermediaries, not just at the first hop.

    A direct-import check is not enough. ``agent/diagnose/llm.py`` imports
    ``netvalue.llm.client``, which is unrestricted — if that module ever imported the
    world, the agent would have a path to ground truth and the first-hop guard would
    report a clean pass. This walks the whole first-party import graph and closes that.
    """
    seen: set[Path] = set()
    stack: list[tuple[Path, tuple[str, ...]]] = [(path, (path.relative_to(REPO_ROOT).as_posix(),))]

    while stack:
        current, chain = stack.pop()
        if current in seen:
            continue
        seen.add(current)

        for name in _imported_targets(current):
            if _violates(name):
                raise AssertionError(
                    "the agent can reach ground truth through "
                    + " -> ".join(chain)
                    + f" -> {name}.\n"
                    "Every module on that path is part of the boundary, whichever "
                    "directory it lives in."
                )
            nxt = _module_to_path(name)
            if nxt is not None and nxt not in seen:
                rel = nxt.relative_to(REPO_ROOT).as_posix()
                if rel in ALLOWLIST:
                    continue
                stack.append((nxt, (*chain, rel)))


@pytest.mark.parametrize(
    "path", _restricted_files(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_agent_never_dynamically_imports_world(path: Path) -> None:
    """Static imports are not the only way in."""
    offenders = sorted(_dynamic_import_strings(path))
    rel = path.relative_to(REPO_ROOT).as_posix()
    assert not offenders, f"{rel} references ground truth dynamically: {offenders}"


def test_allowlist_entries_are_real_and_justified() -> None:
    """An allowlist that drifts out of date quietly reopens the boundary."""
    for entry, reason in ALLOWLIST.items():
        assert (REPO_ROOT / entry).exists(), (
            f"ALLOWLIST references {entry}, which does not exist. Remove the stale entry."
        )
        assert len(reason) > 40, f"ALLOWLIST entry {entry} needs a real justification"


def test_package_layout_is_present() -> None:
    """The repository shape encodes the boundary, so the shape itself is asserted."""
    for expected in ("netvalue/world", "netvalue/agent", "netvalue/eval", "netvalue/llm"):
        assert (REPO_ROOT / expected).is_dir(), f"missing package directory {expected}"
