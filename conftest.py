"""Repo-root pytest hook: keep the coverage gate per tier (instance file, not template-owned).

`addopts` measures `app` and `trustbridge` for the plain run (tests/unit, tests/trustbridge,
tests/integration). The e2e run drives only the template app in a subprocess, so it cannot
execute `trustbridge`; its own 100% gate stays on `app` alone, as before this package existed.
"""

import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Drop `trustbridge` from the measured sources when only tests/e2e is being run."""
    args = [str(arg) for arg in config.invocation_params.args]
    sources = getattr(config.option, "cov_source", None)
    if sources and any("tests/e2e" in arg for arg in args):
        config.option.cov_source = [source for source in sources if source != "trustbridge"]
