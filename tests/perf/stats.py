"""Sample a container's CPU and memory from its cgroup files during a load run.

Usage::

    uv run python tests/perf/stats.py --container <name-or-id> \
        --interval 2 --out tests/perf/.results/stats.json [--duration 120]

Runs until SIGINT/SIGTERM (or ``--duration`` seconds), then writes one JSON
file: ``{"container", "interval_s", "samples": [{"t", "cpu_percent",
"mem_bytes", "cpu_t", "cpu_usage_usec"}], "summary": {"cpu_percent":
{"mean", "p95", "max"}, "mem_bytes": {"mean", "max"}, "mem_peak_bytes",
"cpu_seconds_total", "window_s"}}``.

No docker call is made while sampling: ``docker inspect`` runs once at start
to resolve the full container id, then the container's cgroup v2 directory
(``/sys/fs/cgroup/docker/<id>`` or the systemd ``system.slice/docker-<id>.scope``)
is read directly. ``docker exec``/``docker stats`` every interval froze small
hosts under load. The caller (devcontainer or runner) must be able to see the
container's cgroup; otherwise it fails at start.

``cpu_usage_usec`` is the cgroup's cumulative ``cpu.stat`` ``usage_usec``
taken at ``cpu_t``; ``cpu_percent`` is its delta over wall time (100 = one
CPU), ``mem_bytes`` is ``memory.current``. ``summary.cpu_seconds_total`` is
last minus first ``cpu_usage_usec``, ``summary.window_s`` the matching
``cpu_t`` span (both ``null`` with under two samples). ``summary.mem_peak_bytes``
is ``memory.peak`` at the end (``null`` if the kernel lacks it).
``--since`` / ``--until`` (unix timestamps) restrict the summary to samples
inside that window.
"""

from __future__ import annotations

import argparse
import json
import math
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import FrameType
from typing import Any

CGROUP_ROOT = Path("/sys/fs/cgroup")


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile; 0.0 for an empty list."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]


def summarize(
    samples: list[dict[str, float | int]],
    since: float | None = None,
    until: float | None = None,
    mem_peak_bytes: int | None = None,
) -> dict[str, Any]:
    """Aggregate samples into the ``summary`` block."""
    cpu = [float(s["cpu_percent"]) for s in samples]
    mem = [int(s["mem_bytes"]) for s in samples]
    window = cpu_seconds_between(samples, since, until)
    return {
        "cpu_percent": {
            "mean": sum(cpu) / len(cpu) if cpu else 0.0,
            "p95": percentile(cpu, 95),
            "max": max(cpu, default=0.0),
        },
        "mem_bytes": {
            "mean": sum(mem) / len(mem) if mem else 0.0,
            "max": max(mem, default=0),
        },
        "mem_peak_bytes": mem_peak_bytes,
        "cpu_seconds_total": window[0] if window else None,
        "window_s": window[1] if window else None,
    }


def parse_cpu_stat(text: str) -> int:
    """Return ``usage_usec`` from a cgroup v2 ``cpu.stat`` file's text."""
    for line in text.splitlines():
        key, _, value = line.partition(" ")
        if key == "usage_usec":
            return int(value)
    raise ValueError("no usage_usec in cpu.stat")


def resolve_container_id(container: str) -> str:
    """Resolve a container name or id to its full id (the only docker call)."""
    out = subprocess.run(  # noqa: S603 -- fixed docker argv, container is the operator's own CLI arg
        ["docker", "inspect", "--format", "{{.Id}}", container],  # noqa: S607 -- docker from PATH, as in the devcontainer
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return out.strip()


def find_cgroup_dir(container_id: str, root: Path = CGROUP_ROOT) -> Path:
    """Locate the container's cgroup v2 directory; raise ``FileNotFoundError``."""
    candidates = [
        root / "docker" / container_id,
        root / "system.slice" / f"docker-{container_id}.scope",
    ]
    for path in candidates:
        if (path / "cpu.stat").is_file():
            return path
    tried = ", ".join(str(p) for p in candidates)
    raise FileNotFoundError(f"no cgroup v2 directory for container {container_id} (tried {tried})")


def read_int(path: Path) -> int | None:
    """Read a single-integer cgroup file; ``None`` if absent or ``max``/unparsable."""
    try:
        return int(path.read_text().strip())
    except OSError, ValueError:
        return None


def cpu_seconds_between(
    samples: list[dict[str, float | int]],
    since: float | None = None,
    until: float | None = None,
) -> tuple[float, float] | None:
    """Return ``(cpu_seconds, window_s)`` over samples with ``cpu_t`` in [since, until].

    ``None`` when fewer than two samples carry ``cpu_usage_usec`` in the window.
    For CPU-ms/request: ``cpu_seconds * 1000 / requests in the same window``.
    """
    pts = [
        s
        for s in samples
        if "cpu_usage_usec" in s
        and (since is None or float(s["cpu_t"]) >= since)
        and (until is None or float(s["cpu_t"]) <= until)
    ]
    if len(pts) < 2:
        return None
    first, last = pts[0], pts[-1]
    return (
        (int(last["cpu_usage_usec"]) - int(first["cpu_usage_usec"])) / 1e6,
        float(last["cpu_t"]) - float(first["cpu_t"]),
    )


def sample_once(
    cgroup: Path, prev: tuple[float, int]
) -> tuple[dict[str, float | int], tuple[float, int]]:
    """Read ``cpu.stat`` and ``memory.current``; ``prev`` is ``(time, usage_usec)``."""
    usage = parse_cpu_stat((cgroup / "cpu.stat").read_text())
    now = time.time()
    mem = read_int(cgroup / "memory.current")
    elapsed = now - prev[0]
    percent = (usage - prev[1]) / 1e6 / elapsed * 100 if elapsed > 0 else 0.0
    sample: dict[str, float | int] = {
        "t": now,
        "cpu_percent": percent,
        "mem_bytes": mem if mem is not None else 0,
        "cpu_t": now,
        "cpu_usage_usec": usage,
    }
    return sample, (now, usage)


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument(
        "--container", required=True, help="container name or id (resolved once via docker inspect)"
    )
    parser.add_argument("--interval", type=float, default=2.0, help="seconds between samples")
    parser.add_argument("--out", required=True, type=Path, help="output JSON path")
    parser.add_argument("--duration", type=float, default=None, help="stop after N seconds")
    parser.add_argument("--since", type=float, default=None, help="summary: unix ts window start")
    parser.add_argument("--until", type=float, default=None, help="summary: unix ts window end")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Sample until stopped, then write the JSON file; return the exit code."""
    args = build_parser().parse_args(argv)
    stop = threading.Event()

    def _stop(_signum: int, _frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    try:
        container_id = resolve_container_id(args.container)
        cgroup = find_cgroup_dir(container_id)
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) else exc
        print(f"cannot resolve container cgroup: {detail}", file=sys.stderr)
        return 1

    samples: list[dict[str, float | int]] = []
    prev = (time.time(), parse_cpu_stat((cgroup / "cpu.stat").read_text()))
    started = time.monotonic()
    while not stop.is_set():
        deadline = time.monotonic() + args.interval
        while not stop.is_set() and time.monotonic() < deadline:
            time.sleep(max(0.0, min(0.2, deadline - time.monotonic())))
        if stop.is_set():
            break
        try:
            sample, prev = sample_once(cgroup, prev)
        except (OSError, ValueError) as exc:
            print(f"cgroup read failed: {exc}", file=sys.stderr)
            return 1
        samples.append(sample)
        if args.duration is not None and time.monotonic() - started >= args.duration:
            break
    mem_peak = read_int(cgroup / "memory.peak")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            {
                "container": args.container,
                "interval_s": args.interval,
                "samples": samples,
                "summary": summarize(samples, args.since, args.until, mem_peak),
            },
            indent=2,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
