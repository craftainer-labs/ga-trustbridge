# Installing trustbridge

trustbridge runs as three processes from one image: the **api** (ingress for
GitHub webhooks and the admin API), the **worker** (consumes the Valkey
stream) and the **reconciler** (relay, delivery reconciliation, retention),
plus a **migrate** Job. The Helm chart at
[`deploy/helm/trustbridge`](../deploy/helm/README.md) deploys them. Postgres,
Valkey, an OIDC provider, S3 and an MQTT broker are external; the chart has
no subcharts. The architecture is in
[ADR 0028](adrs/0028-trustbridge-architecture-and-instance-layout.md).

## Prerequisites

- Kubernetes 1.30 or later and Helm.
- **Postgres** reachable over TLS (`database.sslmode` is `verify-full` by
  default; production needs at least `require`). Create the two roles
  before the first install; migrations only `GRANT`
  ([ADR 0031](adrs/0031-tenancy-and-row-level-security.md)):

  ```sql
  CREATE ROLE trustbridge_owner LOGIN PASSWORD '...' NOSUPERUSER NOBYPASSRLS;
  CREATE ROLE trustbridge_app   LOGIN PASSWORD '...' NOSUPERUSER NOBYPASSRLS;
  ALTER DATABASE trustbridge OWNER TO trustbridge_owner;
  ALTER SCHEMA public OWNER TO trustbridge_owner;
  ALTER DEFAULT PRIVILEGES FOR ROLE trustbridge_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO trustbridge_app;
  ALTER DEFAULT PRIVILEGES FOR ROLE trustbridge_owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO trustbridge_app;
  ```

  The runtime role must not be a superuser, have `BYPASSRLS` or own tables:
  the processes report not ready otherwise. The migrate Job connects to the
  primary directly (it holds an advisory lock), not through a transaction
  pooler.
- **Valkey** reachable over TLS (`rediss://`).
- An OIDC provider (https), an S3-compatible endpoint (https) and an MQTT
  broker (TLS, with credentials). The template's settings validation requires
  the last two in production (see the chart README's known limits).
- A way to run the image: `ghcr.io/craftainer-labs/ga-trustbridge` by
  default (`image.registry`, `image.repository`). The default registry is
  `ghcr.io` because the repository variable `OCI_REGISTRY` is not set;
  override it if your release publishes elsewhere.

## Create the Secrets

Secrets are referenced, never put in values. Create them in the release
namespace first (or through your secret manager, for example External Secrets):

```bash
kubectl -n trustbridge create secret generic tb-db-runtime --from-literal=password=...
kubectl -n trustbridge create secret generic tb-db-owner   --from-literal=password=...
kubectl -n trustbridge create secret generic tb-valkey     --from-literal=url='rediss://:...@valkey:6380/0'
kubectl -n trustbridge create secret generic tb-subscriber --from-literal=subscriber-secret="$(openssl rand -hex 32)"
kubectl -n trustbridge create secret generic tb-registration --from-literal=registration-state-key="$(openssl rand -hex 32)"
kubectl -n trustbridge create secret generic tb-s3   --from-literal=access-key=... --from-literal=secret-key=...
kubectl -n trustbridge create secret generic tb-mqtt --from-literal=username=... --from-literal=password=...
```

The key names above are the chart defaults (`passwordKey`, `urlKey`, ...).
The webhook secret and the signing key are not chart values: they are the
files that `GITHUB_HOSTS` (`webhook_secret_ref: file:/...`) and `SIGNERS`
point at, so mount them with `extraVolumes` and `extraVolumeMounts`
(production refuses the `file` signer; use `vault-transit`, whose
credentials are also file paths).

## Values

A minimal production install is `deploy/helm/trustbridge/ci/minimal-values.yaml`.
You must set:

| Value | Why |
| --- | --- |
| `database.host`, `database.runtime.existingSecret`, `database.migrate.existingSecret` | external Postgres, the two roles' passwords |
| `valkey.existingSecret` | `REDIS_URL` |
| `oidc.issuerUrl` | https issuer (and `oidc.discoveryUrl` if the backend reaches it at another address) |
| `publicBaseUrl` | https origin; the webhook URL GitHub is given is `<publicBaseUrl>/webhooks/github/<host name>` |
| `trustbridge.githubHosts`, `trustbridge.signers` | no defaults ([ADR 0030](adrs/0030-key-signer-and-github-hosts.md)) |
| `trustbridge.trustedProxies` | the addresses or CIDRs of your ingress controller; `[]` means no proxy. `*` is refused |
| `s3.*`, `mqtt.*`, `subscriberSecret.existingSecret` | required by the template in production |

Behind an ingress, list it in `trustedProxies`. With an empty list every
client shares the ingress's address and one rate-limit bucket
([ADR 0028](adrs/0028-trustbridge-architecture-and-instance-layout.md),
"Abuse limits"), and the ingress must set `X-Forwarded-For`.
`ingress.className` has no default; set it for your controller.
`registrationStateKey` is optional: without it the GitHub App manifest flow
(host registration) answers 503.

## Install

```bash
helm install trustbridge deploy/helm/trustbridge \
  --namespace trustbridge --create-namespace -f my-values.yaml
```

The chart is installed from this repository's path; publishing it to an OCI
registry is a separate release item. Helm
runs the migrate Job first (a pre-install hook, as `trustbridge_owner`); if it
fails the release fails and nothing else starts. Then the Deployments start.
The api reports not ready until the schema is current and stays alive, so a
slow migration does not restart it; a process exits non-zero only after
`SCHEMA_WAIT_SECONDS` (300).

Verify:

```bash
kubectl -n trustbridge get deploy,pods
kubectl -n trustbridge logs deploy/trustbridge-api | head
```

## Health probes

The api answers `/health/live` and `/health/ready` on its port. The worker
and reconciler serve the same two paths on `HEALTH_PORT` (chart:
`worker.health.port` and `reconciler.health.port`, default 8081), a plain
HTTP responder with no authentication that returns only
`{"status": "ok"}` or `{"status": "fail"}` (readiness adds `ok` or `fail`
per check, `valkey` and `schema`; details go to the log).

- **Liveness** touches no dependency. It fails when a loop made no progress
  for its own interval plus `LEASE_TTL_SECONDS` (125 s for the worker's read
  loop and the relay tick, 1020 s for the 15-minute re-relay sweep). Each
  finished stream entry and each step of a reconciler job counts as progress,
  so a busy but healthy process is not restarted; the kubelet then waits
  `failureThreshold` x `periodSeconds` (60 s) more. A process that is waiting
  for the schema is alive.
- **Readiness** pings Valkey and reads the schema version from Postgres, each
  under a 5 s timeout. The checks run one at a time and their result is reused
  for 2 s, so concurrent probes cost one PING and one read. A pod that is not ready receives no
  traffic, but neither process takes any; it shows in `kubectl get pods` and
  blocks a rolling update from continuing.
- With `HEALTH_PORT` unset (the default outside the chart) the processes
  serve nothing.

## Register the GitHub App

With `registrationStateKey` set, use the admin API's manifest flow (ADR
0030). Otherwise register the App by hand, store its webhook secret where
`webhook_secret_ref` points, and add `app_id` and `webhook_secret_ref` to the
host entry in `trustbridge.githubHosts` (GHES 3.18 has no manifest flow for
enterprise-owned apps). For air-gapped, CA and proxy settings see
[airgap-ca-proxy.md](airgap-ca-proxy.md).

## Security context

Every pod runs with `runAsNonRoot`, a read-only root filesystem (an
`emptyDir` is mounted at `/tmp`), all capabilities dropped, no privilege
escalation and the `RuntimeDefault` seccomp profile (Pod Security
`restricted`), and with resource requests and limits. The image also runs
under an arbitrary UID, so no `runAsUser` is pinned. The ServiceAccount token
is not mounted (`serviceAccount.automount: false`); turn it on only for a
Vault `kubernetes` signer. A NetworkPolicy is available and off by default
(`networkPolicy.enabled`).
