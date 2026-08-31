"""Backward induction verified against brute force on small instances (2 causes, horizon 3).

Enabled in Phase 7. Enumerated now so the test plan is visible from the first commit
rather than invented once the code already exists.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.phase7


@pytest.mark.skip(reason="Phase 7 deliverable")
def test_placeholder() -> None:
    raise AssertionError("unreachable")
