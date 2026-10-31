# syntax=docker/dockerfile:1.28
# Runtime image (builder → runner), the repo's only top-level Dockerfile, in
# the layout template-docker expects (docs/TEMPLATE.md's "Image layer"). The
# devcontainer image is the base-owned .devcontainer/Dockerfile's `develop`
# stage.

# PYTHON_VERSION and DEBIAN_VERSION are also declared in
# .devcontainer/Dockerfile; scripts/check-dockerfile-versions.sh keeps the
# two equal.

# renovate: datasource=python-version depName=python
ARG PYTHON_VERSION=3.14.8
ARG DEBIAN_VERSION=trixie

# renovate: datasource=github-releases depName=astral-sh/uv
ARG UV_VERSION=0.12.17

ARG APP_UID=1000

ARG SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
ARG SSL_CERT_DIR=/etc/ssl/certs

########################################
# builder — installs dependencies into a venv and installs the app
########################################
FROM python:${PYTHON_VERSION}-slim-${DEBIAN_VERSION} AS builder
ARG UV_VERSION
ARG SSL_CERT_FILE
ARG SSL_CERT_DIR

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    SSL_CERT_FILE=${SSL_CERT_FILE} \
    SSL_CERT_DIR=${SSL_CERT_DIR} \
    REQUESTS_CA_BUNDLE=${SSL_CERT_FILE} \
    CURL_CA_BUNDLE=${SSL_CERT_FILE}

COPY scripts/builder.sh /tmp/builder.sh
RUN bash /tmp/builder.sh "$UV_VERSION"

WORKDIR /build
COPY pyproject.toml uv.lock ./
COPY scripts/builder-sync-deps.sh /tmp/builder-sync-deps.sh
RUN --mount=type=cache,target=/root/.cache/uv \
    bash /tmp/builder-sync-deps.sh

COPY src ./src
COPY scripts/builder-sync-app.sh /tmp/builder-sync-app.sh
RUN --mount=type=cache,target=/root/.cache/uv \
    bash /tmp/builder-sync-app.sh

########################################
# runner — minimal runtime image: just the venv, installed non-editable.
# Runs under an arbitrary UID (e.g. OpenShift's restricted SCC) as well as
# a fixed one — see runner-setup.sh and the --chown/--chmod below.
########################################
FROM python:${PYTHON_VERSION}-slim-${DEBIAN_VERSION} AS runner
ARG APP_UID
ARG SSL_CERT_FILE
ARG SSL_CERT_DIR
# Set by `make build` through BUILD_ARGS in image.mk; empty when built any
# other way.
ARG APP_VERSION=
ARG APP_COMMIT=

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_VERSION=${APP_VERSION} \
    APP_COMMIT=${APP_COMMIT} \
    PATH="/opt/venv/bin:${PATH}" \
    HOME=/home/appuser \
    SSL_CERT_FILE=${SSL_CERT_FILE} \
    SSL_CERT_DIR=${SSL_CERT_DIR} \
    REQUESTS_CA_BUNDLE=${SSL_CERT_FILE} \
    CURL_CA_BUNDLE=${SSL_CERT_FILE} \
    MODE=production

# Create the user and the few writable directories first (home, /app/data),
# then copy the app read-only on top: no recursive chgrp/chmod layer, and the
# runtime user can't modify what it runs. Group 0, not the numeric owner, is
# what an arbitrary UID actually gets under OpenShift's restricted SCC.
COPY scripts/runner-setup.sh /tmp/runner-setup.sh
COPY scripts/runner-setup.d/ /tmp/runner-setup.d/
RUN bash /tmp/runner-setup.sh "$APP_UID" && rm -rf /tmp/runner-setup.sh /tmp/runner-setup.d

# Numeric 0550, like the rest of the runner stage: executable and group-0
# readable, not writable by the runtime user.
COPY --chmod=0550 scripts/runner.sh /usr/local/bin/runner.sh
# The venv gets its group-0 ownership through COPY --chown/--chmod rather than
# a recursive RUN chmod, which would be slow over a whole venv. 0550 for the
# same reason as alembic/ below: one numeric mode for a mixed tree, read-only
# to the runtime user (the .py files become executable too, which is harmless).
COPY --from=builder --chown=${APP_UID}:0 --chmod=0550 /opt/venv /opt/venv
WORKDIR /app

# alembic.ini/alembic/ aren't part of the installed app package (see src/README.md's
# "Don't put fixtures/sample data/documentation here"), so they need copying in
# explicitly for app.main's lifespan hook to find at its CWD-relative "alembic.ini"
# and auto-apply pending migrations on startup -- see CLAUDE.md's "Alembic" section.
# Numeric modes, not the symbolic `u=rX,g=rX,o=`: the smoke job builds with
# the CI runner's stock Docker, where the symbolic form left alembic/versions
# without the execute bit (so alembic listed the revisions but couldn't open
# them). 0550 also makes the .py files executable, which is harmless.
COPY --chown=${APP_UID}:0 --chmod=0440 alembic.ini ./
COPY --chown=${APP_UID}:0 --chmod=0550 alembic ./alembic/

USER $APP_UID
EXPOSE 8000
ENTRYPOINT ["runner.sh"]
