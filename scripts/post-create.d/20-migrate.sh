#!/usr/bin/env bash
# Applies pending Alembic migrations to the devcontainer's postgres
# service -- see CLAUDE.md's "Alembic" section.
set -euo pipefail

uv run alembic upgrade head
