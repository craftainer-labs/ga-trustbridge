{{/*
Expand the name of the chart.
*/}}
{{- define "trustbridge.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Fully qualified app name, truncated at 63 characters (DNS naming spec). If the
release name contains the chart name it is used as is.
*/}}
{{- define "trustbridge.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Resource name with a suffix, truncated so the total stays within 63 characters.
Takes a dict with .suffix and .context (the root context).
*/}}
{{- define "trustbridge.resourceName" -}}
{{- $fullname := include "trustbridge.fullname" .context }}
{{- $maxLen := sub 62 (len .suffix) | int }}
{{- printf "%s-%s" (trunc $maxLen $fullname | trimSuffix "-") .suffix | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels. Takes a dict with .root and .component.
*/}}
{{- define "trustbridge.labels" -}}
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
app.kubernetes.io/name: {{ include "trustbridge.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
app.kubernetes.io/version: {{ .root.Chart.AppVersion | quote }}
helm.sh/chart: {{ printf "%s-%s" .root.Chart.Name .root.Chart.Version | replace "+" "_" }}
{{- end }}

{{/*
Selector labels. Takes a dict with .root and .component.
*/}}
{{- define "trustbridge.selectorLabels" -}}
app.kubernetes.io/name: {{ include "trustbridge.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/*
ServiceAccount name: the chart's own when created, else .Values.serviceAccount.name
or the namespace default.
*/}}
{{- define "trustbridge.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "trustbridge.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
The image: <registry>/<repository>@<digest>, or :<tag> (default Chart.appVersion)
without a digest. global.imageRegistry overrides the registry (air-gapped mirrors).
*/}}
{{- define "trustbridge.image" -}}
{{- $image := .Values.image -}}
{{- $registry := default $image.registry (default "" (.Values.global | default dict).imageRegistry) -}}
{{- $repository := printf "%s/%s" $registry $image.repository | trimPrefix "/" -}}
{{- if $image.digest -}}
{{- printf "%s@%s" $repository $image.digest -}}
{{- else -}}
{{- printf "%s:%s" $repository (default .Chart.AppVersion $image.tag) -}}
{{- end -}}
{{- end }}

{{/*
imagePullSecrets: global.imagePullSecrets plus imagePullSecrets.
*/}}
{{- define "trustbridge.imagePullSecrets" -}}
{{- $secrets := concat ((.Values.global | default dict).imagePullSecrets | default list) (.Values.imagePullSecrets | default list) -}}
{{- with $secrets }}
imagePullSecrets:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end }}

{{/*
A secretKeyRef environment entry as a dict. Takes .name, .secret, .key and .what
(the value that names the secret, for the error message).
*/}}
{{- define "trustbridge.secretEnv" -}}
{{- $_ := required (printf "%s is required" .what) .secret -}}
{{- dict "name" .name "valueFrom" (dict "secretKeyRef" (dict "name" .secret "key" .key)) | toJson -}}
{{- end }}

{{/*
The environment of a process, as YAML list items. Takes a dict with .root,
.process (api, worker, reconciler or migrate) and .role (runtime or migrate: which
database role's user and password the process gets).

Every non-secret setting is a plain value; every secret is a secretKeyRef to an
existing Secret. A null or empty value is not set, so the application default applies.
*/}}
{{- define "trustbridge.env" -}}
{{- $root := .root -}}
{{- $v := $root.Values -}}
{{- $tb := $v.trustbridge -}}
{{- $prod := eq $v.mode "production" -}}
{{- if $prod -}}
{{- $_ := required "publicBaseUrl is required when mode=production" $v.publicBaseUrl -}}
{{- $_ := required "oidc.issuerUrl is required when mode=production" $v.oidc.issuerUrl -}}
{{- $_ := required "s3.endpointUrl is required when mode=production" $v.s3.endpointUrl -}}
{{- $_ := required "mqtt.host is required when mode=production" $v.mqtt.host -}}
{{- if kindIs "invalid" $tb.trustedProxies -}}
{{- fail "trustbridge.trustedProxies is required when mode=production (use [] for no proxy; list the ingress controller otherwise)" -}}
{{- end -}}
{{- end -}}
{{- $_ := required "database.host is required" $v.database.host -}}
{{- if not $tb.githubHosts -}}
{{- fail "trustbridge.githubHosts is required (GITHUB_HOSTS has no default)" -}}
{{- end -}}
{{- if not $tb.signers -}}
{{- fail "trustbridge.signers is required (SIGNERS has no default)" -}}
{{- end -}}
{{- $c := dict -}}
{{- $_ := set $c "MODE" $v.mode -}}
{{- $_ := set $c "APP_NAME" $v.app.name -}}
{{- $_ := set $c "RUN_MIGRATIONS_ON_STARTUP" "false" -}}
{{- $_ := set $c "PUBLIC_BASE_URL" $v.publicBaseUrl -}}
{{- $_ := set $c "POSTGRES_HOST" $v.database.host -}}
{{- $_ := set $c "POSTGRES_PORT" $v.database.port -}}
{{- $_ := set $c "POSTGRES_DB" $v.database.name -}}
{{- $_ := set $c "POSTGRES_SSLMODE" $v.database.sslmode -}}
{{- $_ := set $c "DB_POOL_MODE" $v.database.pool.mode -}}
{{- $_ := set $c "DB_POOL_SIZE" $v.database.pool.size -}}
{{- $_ := set $c "DB_MAX_OVERFLOW" $v.database.pool.maxOverflow -}}
{{- $_ := set $c "DB_POOL_RECYCLE" $v.database.pool.recycle -}}
{{- $_ := set $c "DB_CONNECT_TIMEOUT" $v.database.connectTimeout -}}
{{- $_ := set $c "DB_COMMAND_TIMEOUT" $v.database.commandTimeout -}}
{{- if eq .role "migrate" -}}
{{- $_ := set $c "POSTGRES_USER" $v.database.migrate.user -}}
{{- $_ := set $c "MIGRATE_EXPECTED_ROLE" $v.database.migrate.expectedRole -}}
{{- else -}}
{{- $_ := set $c "POSTGRES_USER" $v.database.runtime.user -}}
{{- end -}}
{{- $_ := set $c "OIDC_ISSUER_URL" $v.oidc.issuerUrl -}}
{{- $_ := set $c "OIDC_ISSUER" $v.oidc.issuer -}}
{{- $_ := set $c "OIDC_DISCOVERY_URL" $v.oidc.discoveryUrl -}}
{{- $_ := set $c "OIDC_AUTHORIZATION_URL" $v.oidc.authorizationUrl -}}
{{- $_ := set $c "OIDC_TOKEN_URL" $v.oidc.tokenUrl -}}
{{- $_ := set $c "OIDC_CLIENT_ID" $v.oidc.clientId -}}
{{- $_ := set $c "OIDC_ALGORITHM" $v.oidc.algorithm -}}
{{- $_ := set $c "OIDC_AUDIENCE" $v.oidc.audience -}}
{{- $_ := set $c "OIDC_SCOPES_SUPPORTED" ($v.oidc.scopesSupported | toJson) -}}
{{- $_ := set $c "S3_ENDPOINT_URL" $v.s3.endpointUrl -}}
{{- $_ := set $c "MQTT_HOST" $v.mqtt.host -}}
{{- $_ := set $c "MQTT_PORT" $v.mqtt.port -}}
{{- $_ := set $c "MQTT_KEEPALIVE_SECONDS" $v.mqtt.keepaliveSeconds -}}
{{- $_ := set $c "MQTT_USE_TLS" $v.mqtt.useTls -}}
{{- $_ := set $c "BULK_ACTION_MAX_MATCHED" $v.app.bulkActionMaxMatched -}}
{{- $_ := set $c "RATE_LIMIT_MOCK_TOKEN" $v.app.rateLimitMockToken -}}
{{- $_ := set $c "RATE_LIMIT_BULK_ACTION" $v.app.rateLimitBulkAction -}}
{{- $_ := set $c "ARCHIVE_PURGE_AFTER_DAYS" $v.app.archivePurgeAfterDays -}}
{{- $_ := set $c "GC_FREEZE" $v.app.gcFreeze -}}
{{- $_ := set $c "HEALTH_INFO_ENABLED" $v.app.healthInfoEnabled -}}
{{- $_ := set $c "PROFILING_ENABLED" $v.app.profilingEnabled -}}
{{- $_ := set $c "STATS_CACHE_TTL_SECONDS" $v.app.statsCacheTtlSeconds -}}
{{- $_ := set $c "SSE_KEEPALIVE_SECONDS" $v.app.sseKeepaliveSeconds -}}
{{- $_ := set $c "SECURITY_TXT_CONTACT" $v.securityTxt.contact -}}
{{- $_ := set $c "SECURITY_TXT_EXPIRES" $v.securityTxt.expires -}}
{{- $_ := set $c "SECURITY_TXT_POLICY" $v.securityTxt.policy -}}
{{- $_ := set $c "SECURITY_TXT_PREFERRED_LANGUAGES" $v.securityTxt.preferredLanguages -}}
{{- $_ := set $c "SECURITY_TXT_CANONICAL" $v.securityTxt.canonical -}}
{{- $_ := set $c "GITHUB_HOSTS" ($tb.githubHosts | toJson) -}}
{{- $_ := set $c "SIGNERS" ($tb.signers | toJson) -}}
{{- $_ := set $c "ALLOW_INSECURE_GITHUB_URLS" $tb.allowInsecureGithubUrls -}}
{{- $_ := set $c "ADAPTERS_ENABLED" ($tb.adaptersEnabled | toJson) -}}
{{- if not (kindIs "invalid" $tb.trustedProxies) -}}
{{- $_ := set $c "TRUSTED_PROXIES" ($tb.trustedProxies | toJson) -}}
{{- end -}}
{{- $_ := set $c "WEBHOOK_MAX_BODY_BYTES" $tb.webhook.maxBodyBytes -}}
{{- $_ := set $c "WEBHOOK_SECRET_READ_TIMEOUT_SECONDS" $tb.webhook.secretReadTimeoutSeconds -}}
{{- $_ := set $c "WEBHOOK_SECRET_REFRESH_MIN_SECONDS" $tb.webhook.secretRefreshMinSeconds -}}
{{- $_ := set $c "WEBHOOK_RATE_LIMIT_ENABLED" $tb.webhook.rateLimit.enabled -}}
{{- $_ := set $c "WEBHOOK_RATE_LIMIT_REQUESTS" $tb.webhook.rateLimit.requests -}}
{{- $_ := set $c "WEBHOOK_RATE_LIMIT_WINDOW_SECONDS" $tb.webhook.rateLimit.windowSeconds -}}
{{- $_ := set $c "WEBHOOK_RATE_LIMIT_TIMEOUT_MS" $tb.webhook.rateLimit.timeoutMs -}}
{{- $_ := set $c "WEBHOOK_RATE_LIMIT_EXEMPT_CIDRS" ($tb.webhook.rateLimit.exemptCidrs | toJson) -}}
{{- $_ := set $c "MAX_DELIVERY_ATTEMPTS" $tb.queue.maxDeliveryAttempts -}}
{{- $_ := set $c "RETRY_BACKOFF_BASE_SECONDS" $tb.queue.retryBackoffBaseSeconds -}}
{{- $_ := set $c "RETRY_BACKOFF_MAX_SECONDS" $tb.queue.retryBackoffMaxSeconds -}}
{{- $_ := set $c "BUSY_RETRY_SECONDS" $tb.queue.busyRetrySeconds -}}
{{- $_ := set $c "DISABLED_TENANT_RETRY_SECONDS" $tb.queue.disabledTenantRetrySeconds -}}
{{- $_ := set $c "WORKER_BLOCK_MS" $tb.queue.workerBlockMs -}}
{{- $_ := set $c "WORKER_BATCH_SIZE" $tb.queue.workerBatchSize -}}
{{- $_ := set $c "STREAM_CLAIM_IDLE_SECONDS" $tb.queue.streamClaimIdleSeconds -}}
{{- $_ := set $c "STREAM_TRIM_INTERVAL_SECONDS" $tb.queue.streamTrimIntervalSeconds -}}
{{- $_ := set $c "RELAY_INTERVAL_SECONDS" $tb.queue.relayIntervalSeconds -}}
{{- $_ := set $c "RELAY_BATCH_SIZE" $tb.queue.relayBatchSize -}}
{{- $_ := set $c "RELAY_RECHECK_SECONDS" $tb.queue.relayRecheckSeconds -}}
{{- $_ := set $c "RELAY_RESWEEP_MIN_AGE_SECONDS" $tb.queue.relayResweepMinAgeSeconds -}}
{{- $_ := set $c "DELIVERY_RECONCILE_INTERVAL_SECONDS" $tb.queue.deliveryReconcileIntervalSeconds -}}
{{- $_ := set $c "DELIVERY_RECONCILE_LOOKBACK_HOURS" $tb.queue.deliveryReconcileLookbackHours -}}
{{- $_ := set $c "DELIVERY_RECONCILE_MAX_PER_RUN" $tb.queue.deliveryReconcileMaxPerRun -}}
{{- $_ := set $c "RETENTION_INTERVAL_SECONDS" $tb.queue.retentionIntervalSeconds -}}
{{- $_ := set $c "BODY_RETENTION_PROCESSED_DAYS" $tb.retention.bodyProcessedDays -}}
{{- $_ := set $c "BODY_RETENTION_DEAD_DAYS" $tb.retention.bodyDeadDays -}}
{{- $_ := set $c "DELIVERY_ID_RETENTION_DAYS" $tb.retention.deliveryIdDays -}}
{{- $_ := set $c "DEAD_STREAM_RETENTION_DAYS" $tb.retention.deadStreamDays -}}
{{- $_ := set $c "AUDIT_RETENTION_DAYS" $tb.audit.retentionDays -}}
{{- $_ := set $c "AUDIT_RETENTION_MIN_DAYS" $tb.audit.retentionMinDays -}}
{{- $_ := set $c "AUDIT_EXPORT_LAG_SECONDS" $tb.audit.exportLagSeconds -}}
{{- $_ := set $c "AUDIT_DENIED_WINDOW_SECONDS" $tb.audit.deniedWindowSeconds -}}
{{- $_ := set $c "AUDIT_DENIED_BURST" $tb.audit.deniedBurst -}}
{{- $_ := set $c "AUDIT_DENIED_TIMEOUT_MS" $tb.audit.deniedTimeoutMs -}}
{{- $_ := set $c "LEASE_TTL_SECONDS" $tb.lease.ttlSeconds -}}
{{- $_ := set $c "ADAPTER_CALL_TIMEOUT_SECONDS" $tb.lease.adapterCallTimeoutSeconds -}}
{{- if $tb.secretStore.existingClaim -}}
{{- $_ := set $c "SECRET_STORE_DIR" $tb.secretStore.mountPath -}}
{{- end -}}
{{- $_ := set $c "SCHEMA_WAIT_SECONDS" $tb.schema.waitSeconds -}}
{{- $_ := set $c "SCHEMA_POLL_SECONDS" $tb.schema.pollSeconds -}}
{{- $_ := set $c "HOST_RECHECK_SECONDS" $tb.hosts.recheckSeconds -}}
{{- $_ := set $c "HOST_CHECK_TIMEOUT_SECONDS" $tb.hosts.checkTimeoutSeconds -}}
{{- $_ := set $c "ADMIN_MAX_BODY_BYTES" $tb.admin.maxBodyBytes -}}
{{- $_ := set $c "ADMIN_RATE_LIMIT_ENABLED" $tb.admin.rateLimit.enabled -}}
{{- $_ := set $c "ADMIN_RATE_LIMIT_READ_REQUESTS" $tb.admin.rateLimit.readRequests -}}
{{- $_ := set $c "ADMIN_RATE_LIMIT_MUTATION_REQUESTS" $tb.admin.rateLimit.mutationRequests -}}
{{- $_ := set $c "ADMIN_RATE_LIMIT_EXPORT_REQUESTS" $tb.admin.rateLimit.exportRequests -}}
{{- $_ := set $c "ADMIN_RATE_LIMIT_ANONYMOUS_REQUESTS" $tb.admin.rateLimit.anonymousRequests -}}
{{- $_ := set $c "ADMIN_RATE_LIMIT_WINDOW_SECONDS" $tb.admin.rateLimit.windowSeconds -}}
{{- $_ := set $c "ADMIN_RATE_LIMIT_TIMEOUT_MS" $tb.admin.rateLimit.timeoutMs -}}
{{- $_ := set $c "ADMIN_RATE_LIMIT_EXEMPT_CIDRS" ($tb.admin.rateLimit.exemptCidrs | toJson) -}}
{{- $_ := set $c "RBAC_ALIAS_TEMPLATE_PERSONAS" $tb.rbac.aliasTemplatePersonas -}}
{{- $_ := set $c "RBAC_ROLE_SEPARATOR" $tb.rbac.roleSeparator -}}
{{- if has .process (list "worker" "reconciler") -}}
{{- $_ := set $c "HEALTH_PORT" (index $v .process).health.port -}}
{{- end -}}
{{- $_ := set $c "OTEL_EXPORTER_OTLP_ENDPOINT" $v.otel.endpoint -}}
{{- $_ := set $c "OTEL_SERVICE_NAME" (printf "%s-%s" $v.otel.serviceName .process) -}}
{{- if $v.ca.existingConfigMap -}}
{{- $bundle := printf "%s/%s" $v.ca.mountPath $v.ca.key -}}
{{- $_ := set $c "SSL_CERT_FILE" $bundle -}}
{{- $_ := set $c "REQUESTS_CA_BUNDLE" $bundle -}}
{{- $_ := set $c "CURL_CA_BUNDLE" $bundle -}}
{{- $_ := set $c "AWS_CA_BUNDLE" $bundle -}}
{{- $_ := set $c "PGSSLROOTCERT" $bundle -}}
{{- end -}}
{{- if and (or $v.proxy.httpProxyKey $v.proxy.httpsProxyKey) (not $v.proxy.existingSecret) -}}
{{- fail "proxy.existingSecret is required when proxy.httpProxyKey or proxy.httpsProxyKey is set" -}}
{{- end -}}
{{- if not $v.proxy.httpProxyKey -}}
{{- $_ := set $c "HTTP_PROXY" $v.proxy.httpProxy -}}
{{- $_ := set $c "http_proxy" $v.proxy.httpProxy -}}
{{- end -}}
{{- if not $v.proxy.httpsProxyKey -}}
{{- $_ := set $c "HTTPS_PROXY" $v.proxy.httpsProxy -}}
{{- $_ := set $c "https_proxy" $v.proxy.httpsProxy -}}
{{- end -}}
{{- $_ := set $c "NO_PROXY" $v.proxy.noProxy -}}
{{- $_ := set $c "no_proxy" $v.proxy.noProxy -}}
{{- $env := list -}}
{{- range $k := keys $c | sortAlpha -}}
{{- $val := get $c $k -}}
{{- if not (kindIs "invalid" $val) -}}
{{- if kindIs "float64" $val -}}
{{- if eq $val (floor $val) -}}
{{- $val = printf "%.0f" $val -}}
{{- end -}}
{{- end -}}
{{- $text := toString $val -}}
{{- if ne $text "" -}}
{{- $env = append $env (dict "name" $k "value" $text) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if eq .role "migrate" -}}
{{- $env = append $env (include "trustbridge.secretEnv" (dict "name" "POSTGRES_PASSWORD" "secret" $v.database.migrate.existingSecret "key" $v.database.migrate.passwordKey "what" "database.migrate.existingSecret") | fromJson) -}}
{{- else -}}
{{- $env = append $env (include "trustbridge.secretEnv" (dict "name" "POSTGRES_PASSWORD" "secret" $v.database.runtime.existingSecret "key" $v.database.runtime.passwordKey "what" "database.runtime.existingSecret") | fromJson) -}}
{{- end -}}
{{- $env = append $env (include "trustbridge.secretEnv" (dict "name" "REDIS_URL" "secret" $v.valkey.existingSecret "key" $v.valkey.urlKey "what" "valkey.existingSecret") | fromJson) -}}
{{- if or $prod $v.s3.existingSecret -}}
{{- $env = append $env (include "trustbridge.secretEnv" (dict "name" "RUSTFS_ACCESS_KEY" "secret" $v.s3.existingSecret "key" $v.s3.accessKeyKey "what" "s3.existingSecret") | fromJson) -}}
{{- $env = append $env (include "trustbridge.secretEnv" (dict "name" "RUSTFS_SECRET_KEY" "secret" $v.s3.existingSecret "key" $v.s3.secretKeyKey "what" "s3.existingSecret") | fromJson) -}}
{{- end -}}
{{- if or $prod $v.mqtt.existingSecret -}}
{{- $env = append $env (include "trustbridge.secretEnv" (dict "name" "MQTT_USERNAME" "secret" $v.mqtt.existingSecret "key" $v.mqtt.usernameKey "what" "mqtt.existingSecret") | fromJson) -}}
{{- $env = append $env (include "trustbridge.secretEnv" (dict "name" "MQTT_PASSWORD" "secret" $v.mqtt.existingSecret "key" $v.mqtt.passwordKey "what" "mqtt.existingSecret") | fromJson) -}}
{{- end -}}
{{- if or $prod $v.subscriberSecret.existingSecret -}}
{{- $env = append $env (include "trustbridge.secretEnv" (dict "name" "SUBSCRIBER_SECRET" "secret" $v.subscriberSecret.existingSecret "key" $v.subscriberSecret.key "what" "subscriberSecret.existingSecret") | fromJson) -}}
{{- end -}}
{{- if $v.registrationStateKey.existingSecret -}}
{{- $env = append $env (include "trustbridge.secretEnv" (dict "name" "REGISTRATION_STATE_KEY" "secret" $v.registrationStateKey.existingSecret "key" $v.registrationStateKey.key "what" "registrationStateKey.existingSecret") | fromJson) -}}
{{- end -}}
{{- if and $v.proxy.existingSecret $v.proxy.httpProxyKey -}}
{{- $env = append $env (include "trustbridge.secretEnv" (dict "name" "HTTP_PROXY" "secret" $v.proxy.existingSecret "key" $v.proxy.httpProxyKey "what" "proxy.existingSecret") | fromJson) -}}
{{- $env = append $env (include "trustbridge.secretEnv" (dict "name" "http_proxy" "secret" $v.proxy.existingSecret "key" $v.proxy.httpProxyKey "what" "proxy.existingSecret") | fromJson) -}}
{{- end -}}
{{- if and $v.proxy.existingSecret $v.proxy.httpsProxyKey -}}
{{- $env = append $env (include "trustbridge.secretEnv" (dict "name" "HTTPS_PROXY" "secret" $v.proxy.existingSecret "key" $v.proxy.httpsProxyKey "what" "proxy.existingSecret") | fromJson) -}}
{{- $env = append $env (include "trustbridge.secretEnv" (dict "name" "https_proxy" "secret" $v.proxy.existingSecret "key" $v.proxy.httpsProxyKey "what" "proxy.existingSecret") | fromJson) -}}
{{- end -}}
{{- $env = concat $env ($v.extraEnv | default list) -}}
{{- toYaml $env -}}
{{- end }}

{{/*
Volumes of a pod. Takes a dict with .root and .secretStore (whether to mount the
secret-store claim: not for the migrate Job).
*/}}
{{- define "trustbridge.volumes" -}}
{{- $v := .root.Values -}}
{{- $vols := list (dict "name" "tmp" "emptyDir" (dict)) -}}
{{- if $v.ca.existingConfigMap -}}
{{- $vols = append $vols (dict "name" "ca-bundle" "configMap" (dict "name" $v.ca.existingConfigMap "items" (list (dict "key" $v.ca.key "path" $v.ca.key)))) -}}
{{- end -}}
{{- if and .secretStore $v.trustbridge.secretStore.existingClaim -}}
{{- $vols = append $vols (dict "name" "secret-store" "persistentVolumeClaim" (dict "claimName" $v.trustbridge.secretStore.existingClaim)) -}}
{{- end -}}
{{- $vols = concat $vols ($v.extraVolumes | default list) -}}
{{- toYaml $vols -}}
{{- end }}

{{/*
Volume mounts of a container; same parameters as trustbridge.volumes.
*/}}
{{- define "trustbridge.volumeMounts" -}}
{{- $v := .root.Values -}}
{{- $mounts := list (dict "name" "tmp" "mountPath" "/tmp") -}}
{{- if $v.ca.existingConfigMap -}}
{{- $mounts = append $mounts (dict "name" "ca-bundle" "mountPath" $v.ca.mountPath "readOnly" true) -}}
{{- end -}}
{{- if and .secretStore $v.trustbridge.secretStore.existingClaim -}}
{{- $mounts = append $mounts (dict "name" "secret-store" "mountPath" $v.trustbridge.secretStore.mountPath) -}}
{{- end -}}
{{- $mounts = concat $mounts ($v.extraVolumeMounts | default list) -}}
{{- toYaml $mounts -}}
{{- end }}
