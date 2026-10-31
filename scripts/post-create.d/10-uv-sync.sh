#!/usr/bin/env bash
# Installs the project's dependencies (incl. the dev extra) into the venv
# UV_PROJECT_ENVIRONMENT points at -- see .devcontainer/compose.instance.yml.
set -euo pipefail

uv sync --extra dev
