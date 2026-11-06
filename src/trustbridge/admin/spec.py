"""Print the admin API's OpenAPI snapshot.

`python -m trustbridge.admin.spec > docs/openapi/admin-v1.json` regenerates the committed
snapshot; the drift test fails when it differs from what this prints (ADR 0033 invariants 11 and
12). Regenerate it in the same change as any route or schema change.
"""

import sys

from trustbridge.admin.routes import snapshot_text

if __name__ == "__main__":
    sys.stdout.write(snapshot_text())
