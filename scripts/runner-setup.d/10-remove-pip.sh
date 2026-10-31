#!/usr/bin/env bash
# The runtime needs no package installer: the app lives in /opt/venv, built
# by the builder stage. Removing pip (and the setuptools/wheel the base image
# may carry) drops their vendored libraries from the image, which a scanner
# otherwise reports with CVEs the app never exercises.
# Usage: 10-remove-pip.sh APP_UID   (run by runner-setup.sh, as root)
set -euo pipefail

python -m pip uninstall -y pip setuptools wheel || true
rm -rf /usr/local/lib/python*/site-packages/pip \
    /usr/local/lib/python*/site-packages/pip-*.dist-info \
    /usr/local/lib/python*/site-packages/setuptools* \
    /usr/local/lib/python*/site-packages/pkg_resources \
    /usr/local/lib/python*/site-packages/wheel* \
    /usr/local/bin/pip /usr/local/bin/pip3*
