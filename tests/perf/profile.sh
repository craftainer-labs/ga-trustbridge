#!/usr/bin/env bash
# Record a py-spy flame graph of the server process in the running `api`
# container (started with compose.perf.yml, which grants SYS_PTRACE).
#
# Usage: tests/perf/profile.sh [-d SECONDS] [-c CONTAINER]
#   -d  sampling duration, default 60
#   -c  container name or id, default: compose service `api`
#
# Mechanism: the runner image has no py-spy and we may not change it, so the
# devcontainer venv's py-spy binary is `docker cp`'d into the container and
# run there as root against PID 1 (runner.sh `exec`s uvicorn, so uvicorn is
# PID 1). Root plus the SYS_PTRACE cap from compose.perf.yml is what lets it
# attach; nothing in the image or .devcontainer/ changes. --nonblocking keeps
# py-spy from pausing the app on every sample (which would skew the load run).
set -euo pipefail

duration=60
container=""
while getopts "d:c:" opt; do
  case "$opt" in
    d) duration="$OPTARG" ;;
    c) container="$OPTARG" ;;
    *) echo "usage: $0 [-d SECONDS] [-c CONTAINER]" >&2; exit 2 ;;
  esac
done

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
results="$here/.results"

if [[ -z "$container" ]]; then
  container="$(cd "$repo" && docker compose -f compose.yml -f compose.perf.yml ps -q api)"
fi
[[ -n "$container" ]] || { echo "api container not running" >&2; exit 1; }

pyspy="$(command -v py-spy || true)"
[[ -n "$pyspy" ]] || pyspy="$(cd "$repo" && uv run which py-spy)"

mkdir -p "$results"
out="$results/profile-$(date +%Y%m%dT%H%M%S).speedscope.json"

docker cp "$pyspy" "$container:/tmp/py-spy"
trap 'docker exec -u 0 "$container" rm -f /tmp/py-spy /tmp/profile.json >/dev/null 2>&1 || true' EXIT

docker exec -u 0 "$container" /tmp/py-spy record --pid 1 --nonblocking \
  --format speedscope --duration "$duration" --output /tmp/profile.json
docker cp "$container:/tmp/profile.json" "$out"
echo "wrote $out (open at https://www.speedscope.app)"
