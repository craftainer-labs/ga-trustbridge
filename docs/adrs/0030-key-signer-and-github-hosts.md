# 0030. Pluggable GitHub App key signer, and github.com / GHES / GHE.com host configuration

## Status

Proposed. The user approved the G-api decisions on 2026-11-01; the ADR
becomes Accepted when the implementation (NG3-NG5) merges. Part of the set [0028](0028-trustbridge-architecture-and-instance-layout.md) and
[0029](0029-adapter-plugin-interface.md). The signer interface and the host
configuration schema are operator-facing APIs.

## Context

A GitHub App authenticates by signing a short-lived JWT (RS256, at most
10 minutes) with its private key, then exchanging it for installation
tokens. Best practice is to keep that key out of application memory and
files where possible, and the enterprise baseline requires it (EB-9: "no
long-lived credential where OIDC federation exists; GitHub App private key
behind a pluggable signer, file for dev, Vault Transit / cloud KMS in
production").

githubkit's App strategies (`AppAuthStrategy`,
`AppInstallationAuthStrategy`) take the private key as a PEM string. That
means the library signs in-process and cannot use a remote signer
directly. Hosts also differ:

| Host | REST base URL | Notes |
| --- | --- | --- |
| github.com | `https://api.github.com` | OIDC issuer `https://token.actions.githubusercontent.com` (documented) |
| GHES | `https://HOST/api/v3` | githubkit needs the `/api/v3` suffix in `base_url` |
| GHE.com (data residency) | `https://api.SUBDOMAIN.ghe.com` | API and web hosts live under `SUBDOMAIN.ghe.com` |

The OIDC issuer for GHES and GHE.com is not stated in the pages consulted
(GitHub's OIDC reference documents only github.com's). We therefore never
derive it: it is configuration, verified at setup time against the host's
OIDC discovery document (`<issuer>/.well-known/openid-configuration`).
NG4 confirms the real values on a GHES and a GHE.com instance before the
defaults are documented, and whether tokens from those hosts carry the
`repository_id` and `repository_owner_id` claims that trust is built on
(github.com documents both).

Registration: the GitHub App **manifest flow** lets a customer register
their own App in three steps (POST the manifest to
`/settings/apps/new` or `/organizations/ORG/settings/apps/new`, receive a
temporary `code` on `redirect_url`, exchange it with
`POST /app-manifests/{code}/conversions` for the App id, `pem` and
`webhook_secret`), all within one hour, with an optional `state` for CSRF
protection. GHES 3.18 documents that manifests are not available for
enterprise-owned GitHub Apps.

## Decision

### Signer

```python
from typing import Protocol


class AppKeySigner(Protocol):
    key_id: str  # stable id of the key (fingerprint / KMS key id)

    async def sign_jwt(self, signing_input: bytes) -> bytes: ...
    async def public_key_pem(self) -> str: ...  # for self-test against the App's registered key
```

trustbridge builds the JWT header and claims (`iss`: App id or client
id, `iat` 60 seconds in the past, `exp` at most 9 minutes ahead), asks the
signer for the RS256 signature over `header.payload`, and assembles the
token. Installation tokens are obtained with that App JWT
(`POST /app/installations/{id}/access_tokens`, with `repositories` and
`permissions` for down-scoping), then given to githubkit as a token auth
strategy per call. We do not use githubkit's `AppAuthStrategy` for
signing, because it needs the PEM; we use githubkit for everything after
the token exists.

**Named signers.** Signers are configured in the `SIGNERS` setting, a map
from a signer name to its configuration (JSON in the environment or a
file), and each host selects one by name through `GitHubHostConfig.signer`.
There is no global `SIGNER` setting, so two hosts can use different
backends or different keys (an App is registered per host, so each has its
own key):

```python
class SignerConfig(BaseModel):  # discriminated on "kind"; extra="forbid"
    kind: Literal["file", "vault-transit", "aws-kms", "gcp-kms", "azure-key-vault"]
    # kind-specific fields, e.g. file: path; vault-transit: addr, mount, key, auth.
    # Credentials are file references (secret_id_file, token_file), never inline values.
```

```json
{"dev": {"kind": "file", "path": "/run/secrets/app.pem"},
 "prod": {"kind": "vault-transit", "addr": "https://vault.example", "mount": "transit",
          "key": "trustbridge-ghes-prod", "auth": {"method": "kubernetes", "role": "trustbridge"}}}
```

Validation (see "Startup checks"): every host's `signer` names an entry;
an entry no host uses is a warning; the `file` kind is refused when
`mode=production`.

Implementations, by `kind`:

- `file`: reads a PEM path (dev only; startup warns, and refuses in
  production mode with no override). Permissions are checked.
- `vault-transit`: signs with a Vault Transit RSA key
  (`POST /transit/sign/<key>/sha2-256` with `signature_algorithm=pkcs1v15`,
  `prehashed=false`, then converts Vault's base64 result). Auth via
  Kubernetes auth or AppRole; the key is non-exportable.
- `aws-kms`, `gcp-kms`, `azure-key-vault`: `Sign` with RSASSA_PKCS1_V1_5
  SHA-256. Added after the first release as extras
  (`trustbridge[aws-kms]`), with the same Protocol.

The imported PEM generated by GitHub can be uploaded to Vault/KMS once and
deleted. Key rotation: GitHub allows multiple private keys per App; the
signer exposes `key_id`, and the rollover is: add the new key, switch the
signer config, delete the old key.

The manifest flow returns the PEM to our callback. The callback hands it
straight to the signer's import operation, an operation of the signer
implementation outside the `AppKeySigner` Protocol (which stays
sign-only; for `vault-transit` the Transit key import, for the later KMS
kinds their import APIs), or, for `file`, writes it with mode 0600, and never logs or returns it.
For the webhook secret and client secret the same secret store is used.

### Hosts

```python
@dataclass(frozen=True, slots=True)
class GitHubHostConfig:
    name: str  # tenant-facing key, e.g. "github.com", "ghes-prod"
    kind: HostKind  # ADR 0029: Literal["github.com", "ghes", "ghe.com"]
    web_url: str  # https://github.com | https://HOST | https://SUB.ghe.com
    api_url: str  # https://api.github.com | https://HOST/api/v3 | https://api.SUB.ghe.com
    oidc_issuer: str  # configured, never derived (see Context)
    registry_host: (
        str  # ghcr.io | containers.HOST (GHES) | containers.SUB.ghe.com (GHE.com), as in ADR 0029
    )
    oidc_id_claims: bool  # tokens carry the id claims (repository_owner_id always, repository_id for repo scopes; ADR 0029 A6); no field default: the host loader sets it explicitly (kind-dependent default True for github.com, False for ghes and ghe.com until NG4 confirms)
    app_id: int | None  # None while the host is unregistered (no App yet)
    signer: str  # key into SIGNERS (see "Named signers")
    webhook_secret_ref: str | None  # None while the host is unregistered
    ca_bundle: Path | None = None  # custom CA for this host (EB-5)

    def to_host(
        self,
    ) -> GitHubHost: ...  # ADR 0029's adapter-facing projection: name, kind, api_url, oidc_issuer, oidc_id_claims, registry_host
```

Hosts come from a settings list (`GITHUB_HOSTS`, a file or env JSON). An
entry may omit `app_id` and `webhook_secret_ref`: the host is then
`unregistered` until a registration flow supplies them (see "Registration").
The
loader applies the `oidc_id_claims` default by `kind` (True for
`github.com`, False for `ghes` and `ghe.com`) when an entry omits the key,
and an explicit value always wins.
`CredentialScope.github_host` and `Tenant.github_host` (ADR 0029) are
`to_host()` of this config. The core matches `GitHubHost.kind` against
`Capabilities.github_hosts` and `oidc_hosts` before calling an adapter, so
adapters receive `api_url`, `oidc_issuer` and `registry_host` and do not
branch on the kind themselves. Multiple hosts can be active in one
deployment; a tenant belongs to exactly one.

**Why `oidc_id_claims`.** Trust is established on the `repository_owner_id`
claim and, for repo scopes, `repository_id` (ADR 0029 A6), which GitHub's OIDC reference
lists in its claims table and says do not change when a repository or
owner is renamed. The reference states that the immutable-`sub` rollout
excludes GitHub Enterprise Server, but its claims table has no GHES note
for the two claims and does not mention GHE.com, so whether those hosts
emit them is unconfirmed. Until NG4 verifies it on a live instance, the
flag is false there and the core falls back to a secret credential or a
manual step; it never falls back to the mutable name claims.

### Startup checks and readiness

The template's lifespan is `replace` tier and has no hook. trustbridge
wraps it (ADR 0028, "Lifespan: a trustbridge lifespan wrapping the
template's"), and the same startup code runs in the api lifespan and in
the worker, reconciler and migrate commands. **Decision (G-api item 5,
option a): per-host availability.** Each configured host is in exactly one
state:

| State | Meaning | Behaviour |
| --- | --- | --- |
| `unregistered` | no App id, key or webhook secret yet | serves only the registration flow; its webhook route answers 503; nothing is processed for it |
| `unavailable` | a check failed (signer, GitHub, issuer) | webhooks are still verified, stored and answered 2xx; its events wait in the outbox, uncounted as failures; no GitHub or adapter call is made for it; the failed checks repeat every `HOST_RECHECK_SECONDS` |
| `ready` | all checks passed | normal operation |

**Only invalid settings stop a process.** Checks that need nothing but the
settings are fatal: the process raises and exits non-zero. Checks that
need the signer, the network or GitHub never are; they set the host to
`unavailable` with a reason, whatever the cause (an outage, a wrong key, a
wrong issuer). Per configured host, in this order, the checks

1. validate the settings (fatal): https only except a flagged dev mode,
   `api_url` shape per host kind, every host's `signer` names an entry in
   `SIGNERS`, no `file` signer in production, and `app_id` and
   `webhook_secret_ref` either both set or both absent. A host with both
   absent skips checks 2 and 3 and is `unregistered`;
2. self-test the signer (`unavailable` on failure): a probe signature
   verifies against `public_key_pem()`, and an App JWT accepted by
   `GET /app` returns an `id` equal to the configured `app_id`, which
   proves the signer's key is the one registered on that host;
3. validate the issuer (`unavailable` on failure): the host's OIDC
   discovery document reports the configured `oidc_issuer` (fetched
   through the configured CA and proxy).

The schema gate (ADR 0028, "Processes and database roles") is separate
from these checks and unchanged: it still delays readiness until the
schema is current.

**Readiness.** The signer is **excluded** from the api's readiness: a
signer, Vault or GHES outage must not take the api, and with it the
webhook ingress for every other host, out of rotation. The api's readiness
covers its own dependencies (schema gate, database). Host availability is
reported per host (state and reason) in the admin API and as a metric, and
the signer's health check on the template's health registry, if registered,
does not gate readiness. The checks make no GitHub call per probe; only the
startup run and the recheck do. In the worker an unavailable host's
deliveries are skipped, not the whole consumer: other hosts are processed
normally.

### Registration

- **Manifest flow (default, github.com, GHE.com, org-owned or
  user-owned Apps on GHES).** It also registers the read-only Argo CD App
  (ADR 0029, per tenant) with a different manifest and purpose. The admin
  UI starts the flow per host, with
  the manifest's `hook_attributes.url`, `redirect_url`, `default_events`
  and `default_permissions` generated from the deployment's base URL and
  adapter needs; `state` is a signed one-time value tied to the session
  and checked on return; the code is exchanged server-side within the
  one-hour window. Air-gapped installs work because the browser, not our
  server, reaches the GitHub host.
- **Manual-registration fallback (GHES 3.18 enterprise-owned Apps, or
  any environment where the flow is unavailable).** The admin follows the
  documented steps (a generated checklist of URL, events, permissions),
  uploads the App id, PEM (imported straight into the signer) and webhook
  secret through an authenticated admin endpoint or CLI. The same
  validations apply.

Both paths produce an App id, key and webhook secret for a host that had
none. A host configured with no App is `unregistered` and serves only
this flow; completing the flow makes the next recheck run the checks and
move it to `ready` or `unavailable`, with no restart. Where the result
(App id, signer key reference, webhook secret reference) is persisted is
open (below).

## Invariants NG3 must implement and test

Each is one testable sentence (the shared ones are in ADR 0028, "Invariants
NG3 must implement and test", cited as "0028 invariant n").

1. The signer self-test fails for a signer whose key is not the one
   registered for the host's `app_id`, and passes for the right key.
2. The JWT never has `exp` more than 9 minutes ahead of now nor `iat`
   later than now, and the clock-skew allowance is covered by a test.
3. Startup refuses the process (non-zero exit) for a `SIGNERS` reference
   that names no entry, a `file` signer when `mode=production`, a non-https
   URL outside the flagged dev mode, a wrong `api_url` shape for the host
   kind, and an `app_id` without a `webhook_secret_ref` or the reverse.
4. A host whose discovery document `issuer` differs from the configured
   `oidc_issuer`, or whose signer self-test fails, becomes `unavailable`
   with a reason; the process starts, and its readiness is unaffected.
5. The PEM returned by any manifest flow (trustbridge's own App or an
   Argo CD read-only App) is never logged or returned to the browser, and a
   signer configuration never contains an inline secret value.
6. A host whose `oidc_id_claims` is false is never offered OIDC trust
   (0029 invariants A6 and A7).
7. A signer, Vault, GitHub or issuer failure for one host leaves the api
   process alive and ready, every other host's state unchanged, and
   webhook ingress for every host (except `unregistered`, and subject to
   invariant 11) accepting deliveries.
8. A delivery for an `unavailable` host is neither handled nor counted as
   a failure nor dead-lettered (including by the stream's delivery count,
   0028 invariant 4) while the host is `unavailable`, and is handled
   without a GitHub redelivery once the host is `ready` again: the
   transition to `ready` in a worker re-adds that host's waiting rows
   itself, so they are handled within `HOST_RECHECK_SECONDS` plus normal
   processing and do not wait for the periodic re-relay sweep.
9. Every process that runs the startup checks (api, worker, reconciler)
   rechecks an `unavailable` host every `HOST_RECHECK_SECONDS`, and the
   host becomes `ready` in that process without a restart when its checks
   pass; an
   `unregistered` host becomes `ready` or `unavailable` after the
   registration flow completes, also without a restart.
10. The signer health check, if registered, is registered once however many
    times the lifespan is entered (0028 invariant 16), makes no GitHub call
    per probe, and does not affect the api's readiness.
11. Webhook secrets are resolved on first use and cached in process
    memory, so a secret-store or Vault outage after a successful read does
    not stop signature verification or ingress; a process with no cached
    secret for a host answers 503 for it (its failed delivery is
    redelivered by the reconciler once the host is `ready`).

## Decisions (G-api, 2026-11-01)

Items 1-4 and 6 are approved as proposed; items 5 and 7 are decided as
below.

1. **Signer interface, `SIGNERS` map and `GITHUB_HOSTS` schema**:
   approved.
2. **Backends in the first release**: `file` and `vault-transit`; cloud KMS
   backends are extras after the first release.
3. **Own JWT assembly with a remote signer**, not githubkit's
   `AppAuthStrategy`: approved.
4. **Startup checks via the wrapping lifespan**: approved, with the
   failure policy of item 5 (above, "Startup checks and readiness").
5. **Failure policy and bootstrap: option (a), per-host availability.**
   `unregistered` (serves only the registration flow), `unavailable` (a
   check failed, events wait in the outbox) or `ready`. Only invalid
   settings stop a process. The signer is excluded from the api's
   readiness and reported per host. 0028 item 7 and "Lifespan" are aligned.
6. **`oidc_id_claims`**, false by default on GHES and GHE.com until NG4
   confirms the claims: approved.
7. **Argo CD: option B.** Production uses a separate read-only GitHub App
   (contents: read, metadata), registered through the manifest flow, whose
   key is the only one given to Argo CD (ADR 0029 item 6). Option A
   (reusing trustbridge's key with the `file` signer) is **removed**, not
   kept as a dev path: a path that only works for the dev signer and
   conflicts with EB-9 elsewhere is noise.

Judgement calls made while recording these (not user decisions; revisit if
wrong):

- Failures are classified by what they need: checks on the settings alone
  are fatal; anything needing the signer, network or GitHub (including a
  wrong key or wrong issuer) makes the host `unavailable`.
- `app_id` and `webhook_secret_ref` become optional on `GitHubHostConfig`
  to express `unregistered`; an unregistered host's webhook route answers
  503.
- The recheck interval `HOST_RECHECK_SECONDS` (default 60) is new.

## Consequences

- With a KMS or Vault backend, once the PEM has been imported and
  deleted, the key is not exportable from the application. The manifest
  flow still passes the PEM through the callback's memory (and, for the
  `file` signer, disk) once, until imported. A compromised pod that
  holds signer access can keep minting App JWTs, and from them
  installation tokens (1 hour each) for every tenant, for as long as that
  access lasts; the protection is against key exfiltration, not against
  use of the signer. Revoke the pod's signer access to cut it off. Cost: a network round trip per JWT,
  mitigated by caching installation tokens, and a hard dependency on the
  signer's availability, handled per host (the host is `unavailable`
  while the signer is; the api stays ready).
- We own about 40 lines of JWT assembly and must test clock skew and
  claim limits ourselves.
- Per-host issuer configuration costs the operator one setting but
  avoids guessing; a wrong value fails closed.
- A GHES 3.18 enterprise-owned App needs a manual step; we document it
  instead of hiding it.
- The unverified GHES/GHE.com issuer values and ID-claim availability are
  a known gap, closed in NG4 with live evidence.
- The wrapping lifespan depends on a Starlette attribute (ADR 0028); the
  smoke test is the guard.

## Open issues

- The `oidc_id_claims` default is applied by the host loader, not a dataclass
  default; behaviour for an unknown `kind` and a startup check that the
  default was applied are NG3's.
- How the manifest-flow callback reaches a signer's import operation (it
  is outside the sign-only `AppKeySigner` Protocol) and what happens for a
  signer without import are settled by NG4.
- Where the registration result of a host (App id, signer key reference,
  webhook secret reference) is persisted when the operator's `GITHUB_HOSTS`
  entry is read-only, and how it overlays that entry, are settled by NG4
  (0028 lists no table for it).
- A process that has never read a host's webhook secret while the secret
  store is down cannot verify, so ingress then answers 503 for that host
  only (invariant 11); a short, cache-warming read at startup narrows the
  window and is NG3's to design.
- Host state is per process and the admin API reports the answering
  replica's view; the registration record NG4 designs must be readable by
  every process so each picks a registration up at its next recheck
  (invariant 9).
- The Argo CD read-only App's PEM is not imported into a signer: it is
  handed to Argo CD's cluster Secret (ADR 0029). How the manifest-flow
  callback passes it to the adapter's tenant connection data is settled by
  NG4 and NG5.
- The signer self-test calls `GET /app` per host at startup, an outbound
  call to a configured host only (EB-5); an air-gapped deployment must
  allow it.
- Webhook secrets and client secrets use the same secret store as signer
  credentials; the store interface is NG3's.

## Sources

- Authenticating as a GitHub App (JWT, 10-minute limit):
  https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app
- Installation access tokens:
  https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app
- Registering a GitHub App from a manifest:
  https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest
- GHES 3.18 version of the same page (not available for enterprise-owned
  Apps):
  https://docs.github.com/en/enterprise-server@3.18/apps/sharing-github-apps/registering-a-github-app-from-a-manifest
- GHE.com / data residency:
  https://docs.github.com/en/enterprise-cloud@latest/admin/data-residency/about-github-enterprise-cloud-with-data-residency
- GHE.com network details:
  https://docs.github.com/en/enterprise-cloud@latest/admin/data-residency/network-details-for-ghecom
- GitHub OIDC reference: https://docs.github.com/en/actions/reference/security/oidc
- githubkit configuration (`base_url`, GHES) and authentication:
  https://github.com/yanyongyu/githubkit (docs: usage/getting-started/configuration.md,
  authentication.md, quickstart/connecting-to-github-enterprise.md)
- Vault Transit sign: https://developer.hashicorp.com/vault/api-docs/secret/transit#sign-data
- AWS KMS Sign: https://docs.aws.amazon.com/kms/latest/APIReference/API_Sign.html
- GitHub App best practices:
  https://docs.github.com/en/apps/creating-github-apps/about-creating-github-apps/best-practices-for-creating-a-github-app
