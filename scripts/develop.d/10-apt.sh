#!/usr/bin/env bash
# libpq-dev/postgresql-client/redis-tools: psql/redis-cli for connecting to
# the stack's postgres/redis services (see .devcontainer/stack/postgres and
# .devcontainer/stack/redis's READMEs). Debian's own repo only carries one
# version of each (currently newer than the pinned server images) -- same
# as libpq-dev, there's no exact-version pin available via apt; a newer
# client talking to an older server is standard practice for both tools.
# RustFS's own CLI (rc) isn't an apt package, so it's installed separately
# by 30-rustfs-cli.sh. (xz-utils, which Node's tarballs need, is already
# installed by template-base's own develop.sh.)
set -euo pipefail

apt-get update
apt-get install -y --no-install-recommends libpq-dev postgresql-client redis-tools
apt-get clean
rm -rf /var/lib/apt/lists/*
