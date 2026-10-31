"""Starts coverage measurement in the api subprocess tests/e2e/conftest.py
launches, when COVERAGE_PROCESS_START is set -- see coverage.py's docs on
measuring subprocesses (https://coverage.readthedocs.io/en/latest/subprocess.html).
Found via the PYTHONPATH conftest.py sets on that subprocess, not imported
directly -- Python imports any sitecustomize module on the path at startup.
"""

import coverage

coverage.process_startup()
