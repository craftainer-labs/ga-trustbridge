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

# Each of these must fail with a message that names the missing input.
expect_failure() {
    local message="$1"
    shift
    if helm template tb "$chart" "$@" >"$work/neg.out" 2>&1; then
        echo "expected helm template to fail ($message): $*" >&2
        exit 1
    fi
    if ! grep -qF "$message" "$work/neg.out"; then
        echo "failed without the expected message '$message':" >&2
        cat "$work/neg.out" >&2
        exit 1
    fi
    echo "==> refused as expected: $message"
}
minimal="$chart/ci/minimal-values.yaml"
expect_failure "publicBaseUrl is required" # the defaults alone
expect_failure "trustedProxies is required" -f "$minimal" --set trustbridge.trustedProxies=null
expect_failure "database.runtime.existingSecret is required" -f "$minimal" --set database.runtime.existingSecret=
expect_failure "valkey.existingSecret is required" -f "$minimal" --set valkey.existingSecret=
expect_failure "trustbridge.signers is required" -f "$minimal" --set trustbridge.signers=null
expect_failure "proxy.existingSecret is required" -f "$minimal" --set proxy.httpsProxyKey=https-proxy
expect_failure "nonsense" -f "$minimal" --set nonsense=1 # values.schema.json: unknown top-level key
expect_failure "mode" -f "$minimal" --set mode=mock # values.schema.json
echo "helm chart checks passed"
