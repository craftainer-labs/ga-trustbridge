#!/usr/bin/env bash
# Sets up the `builder` stage: build tooling and uv.
set -euo pipefail

uv_version=$1

apt-get update
apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    build-essential \
    libpq-dev

curl -LsSf "https://releases.astral.sh/github/uv/releases/download/${uv_version}/uv-installer.sh" \
    | env INSTALLER_NO_MODIFY_PATH=1 sh
mv /root/.local/bin/uv /root/.local/bin/uvx /usr/local/bin/

apt-get clean
rm -rf /var/lib/apt/lists/*
