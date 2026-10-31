#!/usr/bin/env bash
# For the pyright-lsp Claude Code plugin — see .claude/README.md.
set -euo pipefail

# renovate: datasource=pypi depName=pyright
PYRIGHT_VERSION=1.1.414

sudo -u vscode env HOME=/home/vscode /usr/local/bin/uv tool install "pyright==${PYRIGHT_VERSION}"
ln -s /home/vscode/.local/bin/pyright /usr/local/bin/pyright
ln -s /home/vscode/.local/bin/pyright-langserver /usr/local/bin/pyright-langserver
