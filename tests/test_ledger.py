"""Ledger conservation: recovered minus costs equals reported net value, exactly, every run.

Enabled in Phase 4. Enumerated now so the test plan is visible from the first commit
rather than invented once the code already exists.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.phase4


@pytest.mark.skip(reason="Phase 4 deliverable")
def test_placeholder() -> None:
    raise AssertionError("unreachable")
