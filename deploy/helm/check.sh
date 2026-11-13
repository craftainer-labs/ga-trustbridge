#!/usr/bin/env bash
# Chart checks (the `helm-chart` prek hook): helm lint, a negative render with
# the default values, and a `helm template` render of every ci/*.yaml values
# file, each checked by assert_render.py. kubeconform runs over the renders
# when it is installed. Needs helm (scripts/develop.d/25-helm.sh).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
chart="$here/trustbridge"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

if ! command -v helm >/dev/null 2>&1; then
    echo "check.sh: helm not found (scripts/develop.d/25-helm.sh installs it; rebuild the devcontainer)" >&2
    exit 1
fi
helm version --short

for values in "$chart"/ci/*.yaml; do
    name="$(basename "$values" .yaml)"
    echo "==> lint $name"
    helm lint --strict "$chart" -f "$values"
    echo "==> render $name"
    helm template tb "$chart" --namespace tb-system -f "$values" >"$work/$name.yaml"
    (cd "$repo" && uv run python -I deploy/helm/assert_render.py "$name" "$work/$name.yaml")
    if command -v kubeconform >/dev/null 2>&1; then
        kubeconform -strict -summary -ignore-missing-schemas "$work/$name.yaml"
    fi
done

# With the health ports null, neither process gets an endpoint, probes or HEALTH_PORT.
echo "==> render health-off"
helm template tb "$chart" --namespace tb-system -f "$chart/ci/minimal-values.yaml" \
    --set worker.health.port=null --set reconciler.health.port=null >"$work/health-off.yaml"
(cd "$repo" && uv run python -I deploy/helm/assert_render.py health-off "$work/health-off.yaml")

# Each of these must fail, and its output must match an extended regex that names the
# problem (a message of the chart's own `required`/`fail`, or the values.schema.json error).
# The output of every refused render is printed, so a CI log shows the wording.
expect_failure() {
    local patterns=()
    while [ "$1" != "--" ]; do
        patterns+=("$1")
        shift
    done
    shift
    if helm template tb "$chart" "$@" >"$work/neg.out" 2>&1; then
        echo "expected helm template to fail: $*" >&2
        exit 1
    fi
    echo "==> refused: helm template $*"
    sed 's/^/    | /' "$work/neg.out"
    local pattern
    for pattern in "${patterns[@]}"; do
        if ! grep -qE -- "$pattern" "$work/neg.out"; then
            echo "failed, but the output does not match /$pattern/" >&2
            exit 1
        fi
    done
    echo "==> matched: ${patterns[*]}"
}
minimal="$chart/ci/minimal-values.yaml"
expect_failure "publicBaseUrl is required" -- # the defaults alone
expect_failure "trustedProxies is required" -- -f "$minimal" --set trustbridge.trustedProxies=null
expect_failure "database.runtime.existingSecret is required" -- -f "$minimal" --set database.runtime.existingSecret=
expect_failure "valkey.existingSecret is required" -- -f "$minimal" --set valkey.existingSecret=
expect_failure "trustbridge.signers is required" -- -f "$minimal" --set trustbridge.signers=null
expect_failure "proxy.existingSecret is required" -- -f "$minimal" --set proxy.httpsProxyKey=https-proxy
# values.schema.json: the output names the schema failure, the offending key and the rule.
# The wording is helm's; CI prints it above, and the patterns are as specific as it allows.
schema="don't meet the specifications of the schema"
expect_failure "$schema" "nonsense" -- -f "$minimal" --set nonsense=1 # unknown top-level key
expect_failure "$schema" "mode" '"production"' '"dev"' -- -f "$minimal" --set mode=mock
expect_failure "$schema" "port" "65535" -- -f "$minimal" --set worker.health.port=70000
echo "helm chart checks passed"
