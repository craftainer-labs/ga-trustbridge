# template-fastapi's values for template-docker's image release Makefile
# (`-include image.mk`); see docs/TEMPLATE.md's "Image layer" and
# docs/adrs/0024-re-parent-onto-template-docker.md. Merge tier: an instance
# edits this file for its own values and a sync keeps them.

# DOCKERFILE and TARGET keep template-docker's defaults (`Dockerfile`,
# `runner`) and ARCHES (`amd64 arm64`) keep template-docker's defaults too;
# OCI_LICENSE is set below (labs products are Apache-2.0).

# The app reads APP_VERSION / APP_COMMIT (Dockerfile ARGs, exposed as env).
# Recursive (`=`) so git runs only in recipes that build, as the Makefile does.
BUILD_ARGS = --build-arg APP_VERSION=$(RELEASE_VERSION) --build-arg APP_COMMIT=$(shell git rev-parse HEAD)

OCI_LICENSE := Apache-2.0
OCI_DESCRIPTION := FastAPI REST service with generic CRUD, OIDC auth and health checks

# Written to dist/ by `unit-tests` below (a release-check step) and attached
# to the release; the image label points at it.
COVERAGE_REPORT := coverage.xml

# Per-arch smoke (`make release-assets`, on a bare CI runner): mock mode
# boots with zero infrastructure and answers /health/live. The real-services
# gate (NFR-0010) is `stack-smoke` below.
IMAGE_SMOKE_PORT := 8000
IMAGE_SMOKE_PATH := /health/live
IMAGE_SMOKE_RUN_ARGS := -e MODE=mock -e ALLOW_MOCK_MODE=1
IMAGE_SMOKE_TIMEOUT := 60

# release-check runs inside the devcontainer, where the stack is up.
# .PHONY so a directory named like a step can't make Make skip it.
.PHONY: unit-tests stack-smoke
release-check: unit-tests stack-smoke

# The tests that were release-check's whole recipe before the image smoke
# tests joined it as prerequisites; also writes the coverage report.
unit-tests:
	mkdir -p dist
	uv run pytest --cov-report=xml:dist/coverage.xml

# NFR-0010: the release image against real Postgres, Redis, S3, Keycloak and
# MQTT, polling /health/ready. The image runs on the devcontainer's own
# Docker daemon, which cannot join the stack's compose network, so each stack
# host name is pinned to the IP the devcontainer resolves it to. MODE=dev:
# production mode demands TLS everywhere, which the plain stack does not
# serve (the smoke.yml workflow covers production mode with a TLS stack).
# POSTGRES_DB passes through from the caller's environment when set.
stack-smoke:
	@args="-e MODE=dev -e POSTGRES_PORT=5432 -e POSTGRES_HOST=postgres -e S3_ENDPOINT_URL=http://s3:9000 -e REDIS_URL=redis://redis:6379/0 -e MQTT_HOST=mqtt -e MQTT_PORT=1883 -e POSTGRES_DB"; \
	for h in postgres redis s3 keycloak mqtt; do \
		ip="$$(getent hosts $$h | awk '{print $$1; exit}')"; \
		test -n "$$ip" || { echo "stack-smoke: cannot resolve $$h" >&2; exit 1; }; \
		args="$$args --add-host=$$h:$$ip"; \
	done; \
	for e in postgres/postgres s3/s3 keycloak/keycloak; do args="$$args --env-file=.devcontainer/stack/$$e.env"; done; \
	IMAGE_SMOKE_RUN_ARGS="$$args" IMAGE_SMOKE_PATH=/health/ready IMAGE_SMOKE_TIMEOUT=150 \
		bash scripts/image-smoke.sh
