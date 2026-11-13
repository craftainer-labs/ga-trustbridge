#!/usr/bin/env bash
# Installs helm for the chart checks (deploy/helm/check.sh, run by the
# `helm-chart` prek hook): lint and `helm template` over deploy/helm/trustbridge.
# Pinned, and verified against the SHA-256 the release publishes next to the
# archive (the same approach as operator-db's 20-k8s-tools.sh).
set -euo pipefail

# renovate: datasource=github-releases depName=helm/helm
HELM_VERSION=4.3.1

case "$(uname -m)" in
x86_64 | amd64) helm_arch=amd64 ;;
aarch64 | arm64) helm_arch=arm64 ;;
*)
    echo "unsupported architecture for helm: $(uname -m)" >&2
    exit 1
    ;;
esac

helm_asset="helm-v${HELM_VERSION}-linux-${helm_arch}.tar.gz"
helm_tmpdir="$(mktemp -d)"
curl -LsSf --retry 3 --retry-all-errors -o "${helm_tmpdir}/${helm_asset}" \
    "https://get.helm.sh/${helm_asset}"
curl -LsSf --retry 3 --retry-all-errors -o "${helm_tmpdir}/${helm_asset}.sha256sum" \
    "https://get.helm.sh/${helm_asset}.sha256sum"
(cd "$helm_tmpdir" && sha256sum -c "${helm_asset}.sha256sum")
tar -xzf "${helm_tmpdir}/${helm_asset}" -C "$helm_tmpdir"
install -m755 "${helm_tmpdir}/linux-${helm_arch}/helm" /usr/local/bin/helm
rm -rf "$helm_tmpdir"
helm version --short
