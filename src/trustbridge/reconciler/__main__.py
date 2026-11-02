"""Shim: `python -m trustbridge.reconciler` runs `python -m trustbridge.worker reconciler`."""

import sys

from trustbridge.worker.__main__ import main

sys.exit(main(["reconciler", *sys.argv[1:]]))
