"""Shim: `python -m trustbridge.migrations upgrade` runs `python -m trustbridge.worker migrate`."""

import sys

from trustbridge.worker.__main__ import main

sys.exit(main(["migrate", *sys.argv[1:]]))
