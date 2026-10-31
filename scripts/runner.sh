#!/usr/bin/env bash
# Entrypoint for the `runner` stage: starts the application.
#
# APP_SERVER selects the ASGI server: `uvicorn` (default) or `granian`.
# Tuning is env-driven and unset by default; see scripts/README.md.
# `exec` keeps the server as PID 1 so it receives SIGTERM directly.
set -euo pipefail

case "${APP_SERVER:-uvicorn}" in
uvicorn)
    # uvicorn's CLI reads UVICORN_* env vars natively (click
    # auto_envvar_prefix), so only the fixed flags are passed here. --loop and
    # --http are explicit so a missing uvloop/httptools wheel fails loudly
    # instead of silently falling back to asyncio/h11.
    args=(app.main:app --host 0.0.0.0 --port 8000 --loop uvloop --http httptools)
    if [[ -n "${GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS:-}" ]]; then
        args+=(--timeout-graceful-shutdown "$GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS")
    fi
    exec uvicorn "${args[@]}"
    ;;
granian)
    # Granian has no graceful-shutdown timeout; its closest equivalent is the
    # time it waits for workers that refuse to stop before killing them.
    if [[ -n "${GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS:-}" ]]; then
        export GRANIAN_WORKERS_KILL_TIMEOUT="${GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS}s"
    fi
    exec granian --interface asgi --host 0.0.0.0 --port 8000 app.main:app
    ;;
*)
    echo "runner.sh: unknown APP_SERVER '${APP_SERVER}' (expected 'uvicorn' or 'granian')" >&2
    exit 64
    ;;
esac
