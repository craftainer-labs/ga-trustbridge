"""Compare a fixed-load Locust ``--summary-json`` against a baseline run's.

Usage::

    uv run python tests/perf/compare_summary.py \
        --baseline baseline/summary.json --current tests/perf/.results/summary.json \
        --max-regression-percent 20

Exit codes: 0 when the aggregate p95 is within the allowed regression (or there
is no usable baseline, which is reported and passes), 1 when it is worse by more
than ``--max-regression-percent``, 2 when the current summary is unusable.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _aggregate_p95(path: Path) -> float | None:
    """Return the summary's aggregate p95 in ms, or None if absent or not positive."""
    data: Any = json.loads(path.read_text())
    if not isinstance(data, dict):
        return None
    aggregate = data.get("aggregate")
    if not isinstance(aggregate, dict):
        return None
    p95 = aggregate.get("p95")
    if isinstance(p95, bool) or not isinstance(p95, int | float) or p95 <= 0:
        return None
    return float(p95)


def compare(baseline: Path, current: Path, max_regression_percent: float) -> tuple[int, str]:
    """Return (exit code, message) for the current run against the baseline."""
    now = _aggregate_p95(current)
    if now is None:
        return 2, f"current summary {current} has no usable aggregate p95"
    if not baseline.is_file():
        return (
            0,
            f"no baseline at {baseline}; skipping the relative check (p95 {now:g} ms)",
        )
    try:
        before = _aggregate_p95(baseline)
    except (OSError, ValueError) as error:
        return 0, f"baseline {baseline} unreadable ({error}); skipping the relative check"
    if before is None:
        return (
            0,
            f"baseline {baseline} has no usable p95; skipping the relative check",
        )
    change = (now - before) / before * 100
    line = (
        f"aggregate p95: baseline {before:g} ms, current {now:g} ms "
        f"({change:+.1f}%, limit +{max_regression_percent:g}%)"
    )
    if change > max_regression_percent:
        return 1, f"REGRESSION {line}"
    return 0, f"OK {line}"


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, print the comparison and return the exit code."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument(
        "--baseline", type=Path, required=True, help="baseline summary JSON (may not exist)"
    )
    parser.add_argument("--current", type=Path, required=True, help="this run's summary JSON")
    parser.add_argument("--max-regression-percent", type=float, default=20.0)
    args = parser.parse_args(argv)
    code, message = compare(args.baseline, args.current, args.max_regression_percent)
    print(message, file=sys.stderr if code == 2 else sys.stdout)
    return code


if __name__ == "__main__":
    sys.exit(main())
