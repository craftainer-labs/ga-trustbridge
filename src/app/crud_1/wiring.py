"""Wires `craftainer_crud` to this app: session factory, config, and rate limiting.

Importing this module installs the crud config (built from `Settings`) and the
app's session factory via `craftainer_crud.config.configure`, and exposes
`bulk_rate_limit`, the decorator every resource router passes to
`build_resource_router(bulk_rate_limit=...)`, `manifest_registry`, the CRUD manifest's
registry, and `stats_cache`, the optional shared
`/stats`/`/predict` cache (None unless `STATS_CACHE_TTL_SECONDS` is set) routers pass as
`stats_cache=` and `app.main`'s lifespan keeps invalidated and closes. Resource modules
import it, so the config is always installed before any router is built or request served -- even
for tests that import a resource module without `app.main`.
"""

from craftainer_crud.config import CrudConfig, MQTTConfig, configure
from craftainer_crud.controllers.crud_router import RateLimit
from craftainer_crud.manifest import ManifestRegistry

from app.cache import build_stats_cache
from app.config import get_settings
from app.db import session_factory
from app.rate_limit import limiter

_settings = get_settings()

configure(
    CrudConfig(
        use_memory=_settings.mode == "mock",
        bulk_action_max_matched=_settings.bulk_action_max_matched,
        sse_keepalive_seconds=_settings.sse_keepalive_seconds,
        mqtt=MQTTConfig(
            host=_settings.mqtt_host,
            port=_settings.mqtt_port,
            keepalive_seconds=_settings.mqtt_keepalive_seconds,
            username=_settings.mqtt_username,
            password=_settings.mqtt_password,
            use_tls=_settings.mqtt_use_tls,
        ),
        subscriber_secret=(
            _settings.subscriber_secret.encode() if _settings.subscriber_secret else None
        ),
    ),
    session_factory,
)

bulk_rate_limit: RateLimit = limiter.limit(_settings.rate_limit_bulk_action)

stats_cache = build_stats_cache(_settings)

# Every resource router registers itself here (via its `manifest=ManifestOptions(...)`), and
# app.main hands the same registry to `crud_manifest_router`, so GET /api/_crud/manifest
# describes exactly the routers that were built.
manifest_registry = ManifestRegistry()
