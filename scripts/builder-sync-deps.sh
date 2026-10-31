#!/usr/bin/env bash
# Installs locked dependencies only, without the project itself, so a
# source change alone doesn't invalidate this layer.
set -euo pipefail

uv sync --locked --no-install-project --no-editable
