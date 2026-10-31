#!/usr/bin/env bash
# Installs the project itself on top of the dependencies
# builder-sync-deps.sh already synced, non-editable.
set -euo pipefail

uv sync --locked --no-editable
