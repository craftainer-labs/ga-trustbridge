# deploy/helm/

The Helm chart that runs trustbridge's three processes on Kubernetes
(`trustbridge/`), and its checks. Install, upgrade, backup and the air-gap, CA
and proxy setup are in [`docs/`](../../docs/README.md) (`install.md`,
`upgrade.md`, `backup-restore.md`, `airgap-ca-proxy.md`).

- `trustbridge/` — the chart. One Deployment each for the **api**, the
  **worker** and the **reconciler** (same image, different `command`; the
  image's own entrypoint serves the template app, ADR 0028), a **migrate**
  Job run as a Helm pre-install/pre-upgrade hook, the api Service and
  Ingress, one PodDisruptionBudget per process, a ServiceAccount, and an
  optional NetworkPolicy. Postgres and Valkey are external: there are no
  subcharts.
- `trustbridge/ci/*.yaml` — values files for the checks (placeholders, not
  credentials). `full-values` turns everything on (air-gap registry and
  digest, CA, proxy, ingress, NetworkPolicy, OTLP); `minimal-values` is the
  smallest production install.
- `check.sh` — `helm lint --strict` and `helm template` for each `ci/` file,
  `assert_render.py` over the render, and negative renders (a missing
  required value must fail with a message that names it). `kubeconform` runs
  too when it is installed. The `helm-chart` prek hook runs it; helm comes
  from `scripts/develop.d/25-helm.sh`.
- `assert_render.py` — the assertions: Pod Security `restricted` settings on
  every pod, secrets only by `secretKeyRef`, the commands, probes, the
  air-gap, CA and proxy values, and that `_helpers.tpl` maps every
  environment variable `trustbridge.settings` reads.

## Conventions

- Layout and values naming follow the operator charts (`global.imageRegistry`,
  `image.{registry,repository,digest,tag,pullPolicy}`, `imagePullSecrets`,
  `nameOverride`/`fullnameOverride`, `serviceAccount`, `podSecurityContext`,
  `securityContext`, `resources`).
- A secret value never appears in `values.yaml`. Each secret is an
  `existingSecret` plus a key name.
- Every setting is one entry in the `trustbridge.env` helper
  (`templates/_helpers.tpl`). A null or empty value is not set, so the
  application default applies. Add a new setting there and to `values.yaml`;
  `assert_render.py` fails when the application reads a variable the chart
  does not map.
- Production-only requirements (`mode: production`, the default) are checked
  at render time with a message that names the value, mirroring the settings
  validation: `publicBaseUrl`, `oidc.issuerUrl`, `s3.endpointUrl`,
  `mqtt.host`, `trustbridge.trustedProxies` (`[]` means no proxy),
  `subscriberSecret`, the S3 and MQTT secrets. `RUN_MIGRATIONS_ON_STARTUP` is
  always `false` and `allow_mock_mode` is not offered.

## Settings to values

| Environment variable(s) | Value | Notes |
| --- | --- | --- |
| `MODE` | `mode` | `production` (default) or `dev`; `mock` is refused by the values schema |
| `APP_NAME` | `app.name` | |
| `RUN_MIGRATIONS_ON_STARTUP` | (fixed `false`) | production refuses `true`; the migrate Job migrates |
| `PUBLIC_BASE_URL` | `publicBaseUrl` | https origin, required in production |
| `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_SSLMODE` | `database.host`, `.port`, `.name`, `.sslmode` | production needs `require`, `verify-ca` or `verify-full`; there is no DSN setting |
| `POSTGRES_USER`, `POSTGRES_PASSWORD` | `database.runtime.user`, `.existingSecret`, `.passwordKey` | api, worker, reconciler (role `trustbridge_app`) |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `MIGRATE_EXPECTED_ROLE` | `database.migrate.user`, `.existingSecret`, `.passwordKey`, `.expectedRole` | the migrate Job (role `trustbridge_owner`) |
| `DB_POOL_MODE`, `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_POOL_RECYCLE`, `DB_CONNECT_TIMEOUT`, `DB_COMMAND_TIMEOUT` | `database.pool.mode`, `.size`, `.maxOverflow`, `.recycle`; `database.connectTimeout`, `.commandTimeout` | |
| `REDIS_URL` | `valkey.existingSecret`, `.urlKey` | `rediss://` in production |
| `OIDC_ISSUER_URL`, `OIDC_ISSUER`, `OIDC_DISCOVERY_URL`, `OIDC_AUTHORIZATION_URL`, `OIDC_TOKEN_URL`, `OIDC_CLIENT_ID`, `OIDC_ALGORITHM`, `OIDC_AUDIENCE`, `OIDC_SCOPES_SUPPORTED` | `oidc.issuerUrl`, `.issuer`, `.discoveryUrl`, `.authorizationUrl`, `.tokenUrl`, `.clientId`, `.algorithm`, `.audience`, `.scopesSupported` | https in production |
| `REGISTRATION_STATE_KEY` | `registrationStateKey.existingSecret`, `.key` | at least 32 characters; unset: host registration answers 503 |
| `SUBSCRIBER_SECRET` | `subscriberSecret.existingSecret`, `.key` | at least 32 characters; required in production |
| `S3_ENDPOINT_URL`, `RUSTFS_ACCESS_KEY`, `RUSTFS_SECRET_KEY` | `s3.endpointUrl`, `.existingSecret`, `.accessKeyKey`, `.secretKeyKey` | the template requires them in production (see known limits) |
| `MQTT_HOST`, `MQTT_PORT`, `MQTT_KEEPALIVE_SECONDS`, `MQTT_USE_TLS`, `MQTT_USERNAME`, `MQTT_PASSWORD` | `mqtt.host`, `.port`, `.keepaliveSeconds`, `.useTls`, `.existingSecret`, `.usernameKey`, `.passwordKey` | the template requires TLS and credentials in production |
| `BULK_ACTION_MAX_MATCHED`, `RATE_LIMIT_MOCK_TOKEN`, `RATE_LIMIT_BULK_ACTION`, `ARCHIVE_PURGE_AFTER_DAYS`, `GC_FREEZE`, `HEALTH_INFO_ENABLED`, `PROFILING_ENABLED`, `STATS_CACHE_TTL_SECONDS`, `SSE_KEEPALIVE_SECONDS` | `app.bulkActionMaxMatched`, `.rateLimitMockToken`, `.rateLimitBulkAction`, `.archivePurgeAfterDays`, `.gcFreeze`, `.healthInfoEnabled`, `.profilingEnabled`, `.statsCacheTtlSeconds`, `.sseKeepaliveSeconds` | template settings |
| `SECURITY_TXT_CONTACT`, `_EXPIRES`, `_POLICY`, `_PREFERRED_LANGUAGES`, `_CANONICAL` | `securityTxt.contact`, `.expires`, `.policy`, `.preferredLanguages`, `.canonical` | |
| `GITHUB_HOSTS`, `SIGNERS` | `trustbridge.githubHosts`, `trustbridge.signers` | required, no default; JSON of references and file paths, never secret values |
| `ALLOW_INSECURE_GITHUB_URLS`, `ADAPTERS_ENABLED`, `TRUSTED_PROXIES` | `trustbridge.allowInsecureGithubUrls`, `.adaptersEnabled`, `.trustedProxies` | `TRUSTED_PROXIES` is required in production; `[]` means no proxy |
| `WEBHOOK_MAX_BODY_BYTES`, `WEBHOOK_SECRET_READ_TIMEOUT_SECONDS`, `WEBHOOK_SECRET_REFRESH_MIN_SECONDS` | `trustbridge.webhook.maxBodyBytes`, `.secretReadTimeoutSeconds`, `.secretRefreshMinSeconds` | |
| `WEBHOOK_RATE_LIMIT_ENABLED`, `_REQUESTS`, `_WINDOW_SECONDS`, `_TIMEOUT_MS`, `_EXEMPT_CIDRS` | `trustbridge.webhook.rateLimit.enabled`, `.requests`, `.windowSeconds`, `.timeoutMs`, `.exemptCidrs` | |
| `MAX_DELIVERY_ATTEMPTS`, `RETRY_BACKOFF_BASE_SECONDS`, `RETRY_BACKOFF_MAX_SECONDS`, `BUSY_RETRY_SECONDS`, `DISABLED_TENANT_RETRY_SECONDS`, `WORKER_BLOCK_MS`, `WORKER_BATCH_SIZE`, `STREAM_CLAIM_IDLE_SECONDS`, `STREAM_TRIM_INTERVAL_SECONDS`, `RELAY_INTERVAL_SECONDS`, `RELAY_BATCH_SIZE`, `RELAY_RECHECK_SECONDS`, `RELAY_RESWEEP_MIN_AGE_SECONDS`, `DELIVERY_RECONCILE_INTERVAL_SECONDS`, `DELIVERY_RECONCILE_LOOKBACK_HOURS`, `DELIVERY_RECONCILE_MAX_PER_RUN`, `RETENTION_INTERVAL_SECONDS` | `trustbridge.queue.*` (same names in camelCase) | |
| `BODY_RETENTION_PROCESSED_DAYS`, `BODY_RETENTION_DEAD_DAYS`, `DELIVERY_ID_RETENTION_DAYS`, `DEAD_STREAM_RETENTION_DAYS` | `trustbridge.retention.bodyProcessedDays`, `.bodyDeadDays`, `.deliveryIdDays`, `.deadStreamDays` | |
| `AUDIT_RETENTION_DAYS`, `AUDIT_RETENTION_MIN_DAYS`, `AUDIT_EXPORT_LAG_SECONDS`, `AUDIT_DENIED_WINDOW_SECONDS`, `AUDIT_DENIED_BURST`, `AUDIT_DENIED_TIMEOUT_MS` | `trustbridge.audit.*` | |
| `LEASE_TTL_SECONDS`, `ADAPTER_CALL_TIMEOUT_SECONDS` | `trustbridge.lease.ttlSeconds`, `.adapterCallTimeoutSeconds` | the timeout must be shorter than the TTL (validated by the app) |
| `SECRET_STORE_DIR` | `trustbridge.secretStore.existingClaim`, `.mountPath` | set only when a claim is given |
| `SCHEMA_WAIT_SECONDS`, `SCHEMA_POLL_SECONDS`, `HOST_RECHECK_SECONDS`, `HOST_CHECK_TIMEOUT_SECONDS` | `trustbridge.schema.waitSeconds`, `.pollSeconds`; `trustbridge.hosts.recheckSeconds`, `.checkTimeoutSeconds` | |
| `ADMIN_MAX_BODY_BYTES`, `ADMIN_RATE_LIMIT_ENABLED`, `_READ_REQUESTS`, `_MUTATION_REQUESTS`, `_EXPORT_REQUESTS`, `_ANONYMOUS_REQUESTS`, `_WINDOW_SECONDS`, `_TIMEOUT_MS`, `_EXEMPT_CIDRS` | `trustbridge.admin.maxBodyBytes`; `trustbridge.admin.rateLimit.*` | |
| `RBAC_ALIAS_TEMPLATE_PERSONAS`, `RBAC_ROLE_SEPARATOR` | `trustbridge.rbac.aliasTemplatePersonas`, `.roleSeparator` | |
| `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `AWS_CA_BUNDLE`, `PGSSLROOTCERT` | `ca.existingConfigMap`, `.key`, `.mountPath` | all five point at the one mounted bundle |
| `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY` (and lowercase) | `proxy.httpProxy`, `.httpsProxy`, `.noProxy`; or `proxy.existingSecret` with `.httpProxyKey`, `.httpsProxyKey` | |
| `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_SERVICE_NAME` | `otel.endpoint`, `otel.serviceName` | no export unless an endpoint is set |

## Known limits

- **S3 and MQTT are required in production** by the template's settings
  validation, which every process runs, even though trustbridge does not use
  them beyond the template's example resource and health checks. The chart
  therefore requires `s3.*` and `mqtt.*` in production. Changing that is an
  application change.
- **No probes on the worker and reconciler.** They have no HTTP endpoint, so
  the Deployments rely on the process exiting (and Kubernetes restarting it)
  when it cannot continue, for example after `SCHEMA_WAIT_SECONDS`. A health
  endpoint for them is a follow-up.
- **Replicas are 1 for every process.** Scaling beyond that (the worker is a
  consumer group, the reconciler runs singleton duties on one replica at a
  time, ADR 0028) has not been decided; the host state is per process (ADR 0028).
- **The migrate hook runs before the chart's own resources exist.** It uses
  the namespace default ServiceAccount (no token) and reads only existing
  Secrets and ConfigMaps, so the CA bundle ConfigMap is `existingConfigMap`
  only and the chart creates no Secrets.
- **The secret store claim is shared.** `trustbridge.secretStore.existingClaim`
  is mounted into all three Deployments; with more than one pod it must be a
  ReadWriteMany claim.

## Don't

- Put a credential, a webhook secret or a signing key in a values file.
- Add a subchart for Postgres or Valkey.
- Add a setting to `settings.py` without mapping it in `_helpers.tpl`:
  `check.sh` fails.
