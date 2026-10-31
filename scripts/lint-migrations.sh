#!/usr/bin/env bash
# Lint new or changed Alembic revisions with Squawk (NFR-0027).
#
# Usage: bash scripts/lint-migrations.sh [--changed-only] alembic/versions/<rev>_<slug>.py...
#
# Each revision is rendered to SQL with Alembic's offline mode
# (`alembic upgrade <down_revision>:<revision> --sql`, so no database is
# needed) and the SQL is piped to `squawk`, which flags locking and
# rewriting operations that would stall a live database during a rollout.
# Applied migrations are frozen (NFR-0019), so only new or changed
# revisions are linted. The prek hook passes `--changed-only`, which keeps
# just the given files that differ from the merge base with origin/main (or
# main), including staged, unstaged and untracked ones; when neither branch
# exists (a shallow CI clone) nothing counts as changed. Without the flag,
# every given file is linted (migration-compat.yml does this with its own
# list of changed files). Exits non-zero if any revision has findings or
# cannot be rendered.
#
# SQUAWK_ARGS: extra squawk arguments, e.g. SQUAWK_ARGS="--exclude=ban-drop-column".
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

changed_only=false
if [ "${1:-}" = "--changed-only" ]; then
    changed_only=true
    shift
fi

if [ "$#" -eq 0 ] && [ "$changed_only" = false ]; then
    echo "usage: $0 [--changed-only] alembic/versions/<revision>_<slug>.py..." >&2
    exit 64
fi

if [ "$changed_only" = true ]; then
    base="$(git merge-base HEAD origin/main 2>/dev/null || git merge-base HEAD main 2>/dev/null || true)"
    if [ -z "$base" ]; then
        echo "lint-migrations: no origin/main or main to compare against; nothing to lint"
        exit 0
    fi
    changed="$( { git diff --name-only --diff-filter=AMR "$base" -- alembic/versions
                  git ls-files --others --exclude-standard -- alembic/versions; } | sort -u)"
    selected=()
    for file in "$@"; do
        if printf '%s\n' "$changed" | grep -qxF -- "$file"; then
            selected+=("$file")
        fi
    done
    set -- "${selected[@]+"${selected[@]}"}"
    if [ "$#" -eq 0 ]; then
        echo "lint-migrations: no new or changed revisions"
        exit 0
    fi
fi

status=0
for file in "$@"; do
    # Read revision and down_revision from the file itself (ast, not import:
    # importing a revision pulls in alembic.op). A merge revision has a tuple
    # down_revision; it is rendered from base, which covers every parent.
    if ! range="$(uv run --quiet python -I - "$file" <<'PY'
import ast
import sys

values = {}
for node in ast.parse(open(sys.argv[1]).read()).body:
    if isinstance(node, ast.AnnAssign | ast.Assign):
        target = node.target if isinstance(node, ast.AnnAssign) else node.targets[0]
        if isinstance(target, ast.Name) and target.id in ("revision", "down_revision"):
            values[target.id] = ast.literal_eval(node.value)
down = values.get("down_revision")
if not isinstance(down, str):
    down = "base"
print(f"{down}:{values['revision']}")
PY
    )"; then
        echo "lint-migrations: cannot read revision ids from $file" >&2
        status=1
        continue
    fi

    echo "lint-migrations: $file ($range)"
    # stderr carries alembic's INFO log lines, not SQL.
    if ! sql="$(uv run --quiet alembic upgrade "$range" --sql 2>/dev/null)"; then
        echo "lint-migrations: offline rendering failed for $file" >&2
        status=1
        continue
    fi
    # shellcheck disable=SC2086  # SQUAWK_ARGS is deliberately word-split.
    if ! printf '%s\n' "$sql" | uv run --quiet squawk ${SQUAWK_ARGS:-}; then
        status=1
    fi
done

exit "$status"
