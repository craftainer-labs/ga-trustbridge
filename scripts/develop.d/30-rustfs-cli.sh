#!/usr/bin/env bash
# For connecting to the s3 stack service (RustFS) -- see
# .devcontainer/stack/s3/README.md. Published as .deb/.rpm release assets
# plus a SHA256SUMS file, not on apt/PyPI/npm, so fetched and
# checksum-verified against the project's own published SHA256SUMS instead
# of trusting a curl-pipe-to-sh installer (same approach as snip in
# template-base's develop.sh).
set -euo pipefail

# renovate: datasource=github-releases depName=rustfs/cli
RUSTFS_CLI_VERSION=0.1.36

rustfs_cli_arch="$(dpkg --print-architecture)"
rustfs_cli_asset="rustfs-cli_${RUSTFS_CLI_VERSION}_${rustfs_cli_arch}.deb"
rustfs_cli_tmpdir="$(mktemp -d)"
curl -LsSf -o "${rustfs_cli_tmpdir}/${rustfs_cli_asset}" \
    "https://github.com/rustfs/cli/releases/download/v${RUSTFS_CLI_VERSION}/${rustfs_cli_asset}"
curl -LsSf -o "${rustfs_cli_tmpdir}/SHA256SUMS" \
    "https://github.com/rustfs/cli/releases/download/v${RUSTFS_CLI_VERSION}/SHA256SUMS"
(cd "$rustfs_cli_tmpdir" && grep " ${rustfs_cli_asset}\$" SHA256SUMS | sha256sum -c -)
apt-get install -y --no-install-recommends "${rustfs_cli_tmpdir}/${rustfs_cli_asset}"
rm -rf "$rustfs_cli_tmpdir"
