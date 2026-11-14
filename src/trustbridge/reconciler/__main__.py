"""`python -m trustbridge.reconciler`: the reconciler with the tenant deletion composed in."""

import sys

from trustbridge.composition import main

sys.exit(main("reconciler"))
