"""`python -m trustbridge.worker [worker|reconciler|migrate ...]` (default: worker)."""

import sys

from craftainer_observability.logs import configure_logging

from trustbridge.worker import migrate, runtime


def main(argv: list[str]) -> int:
    """Dispatch the sub-command and return the process exit code."""
    configure_logging()
    command = argv[0] if argv else "worker"
    if command == "migrate":
        return migrate.main(argv[1:])
    if command in {"worker", "reconciler"}:
        return runtime.main(command)  # type: ignore[arg-type]
    sys.stderr.write("usage: python -m trustbridge.worker [worker|reconciler|migrate upgrade]\n")
    return 2


if __name__ == "__main__":  # pragma: no cover -- exercised by the shims and the commands
    sys.exit(main(sys.argv[1:]))
