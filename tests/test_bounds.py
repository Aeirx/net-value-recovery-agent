"""Property tests: the gate layer must never be violated, under any input.

Also asserts monotonicity - higher annoyance never increases contact count, higher amount
never decreases willingness to attempt.

Enabled in Phase 7. Enumerated now so the test plan is visible from the first commit
rather than invented once the code already exists.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.phase7


@pytest.mark.skip(reason="Phase 7 deliverable")
def test_placeholder() -> None:
    raise AssertionError("unreachable")
