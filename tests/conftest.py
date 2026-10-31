"""Session-wide fixtures shared by tests/unit and tests/integration."""

from collections.abc import Iterator

import pytest

from app.rate_limit import limiter


@pytest.fixture(autouse=True, scope="session")
def _disable_rate_limiting() -> Iterator[None]:
    """Disable app.rate_limit.limiter for the whole unit/integration run.

    It's backed by the same real, shared Redis instance tests/integration already
    talks to, keyed by client address -- every test in this run shares one
    "testclient" bucket, so without this, an earlier test's bulk/mock-token calls
    would spuriously 429 later, unrelated tests. tests/unit/test_rate_limit.py
    verifies the enforcement itself against a throwaway Limiter/app instead of this
    shared one.
    """
    limiter.enabled = False
    yield
    limiter.enabled = True
