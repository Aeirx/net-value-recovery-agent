"""Same seed, identical output hash. Includes the LLM cache replay path.

Enabled in Phase 3. Enumerated now so the test plan is visible from the first commit
rather than invented once the code already exists.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.phase3


@pytest.mark.skip(reason="Phase 3 deliverable")
def test_placeholder() -> None:
    raise AssertionError("unreachable")
