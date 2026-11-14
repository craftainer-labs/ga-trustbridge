# adapters/

The adapter plugin interface (I2, [ADR 0029](../../../docs/adrs/0029-adapter-plugin-interface.md)) and
the core that drives adapters. Each platform adapter (JFrog, Harbor/MSR, Docker Hub, Argo CD, GHCR)
is a separate package under `adapters/<name>/` (NG5) that registers itself in the
`trustbridge.adapters` entry-point group.

- `interface.py` — the public API: dataclasses, `ChangeState`, `CredentialState`/`CredentialSlot`,
  `CredentialOutcome`, `PlatformAdapter`, `AdapterError`, `AdapterConfig`, `API_VERSION`, and the
  naming helpers (`scope_key`, `credential_name`, `resource_name`). Additive changes only;
  a breaking change bumps `API_VERSION`. `CredentialScope.platform_repository_key` (the `RepoSpec.key` a credential is limited to; `None` keeps the project-wide grant narrowed by claims) and `CredentialScope.oidc_eligible` are additive fields: the
  core sets it, and an adapter returns an `OidcTrust` exactly when it is true.
- `loader.py` — entry-point discovery (`ADAPTERS_ENABLED` only), the `api_version` range
  (`MIN_SUPPORTED_API_VERSION..API_VERSION`) and capability validation (A7).
- Admin API (ADR 0033): the api process loads only the adapters named by `ADAPTERS_ENABLED` through
  `loader.load_adapter_classes` (`trustbridge.main`) and `GET /tenants/{t}/adapters` reports their
  capabilities (read from the class, `capabilities()` is a constant) and whether a tenant has
  connection data (a listing of the secret store, never a value). No adapter behaviour changes.
- `core.py` — `CredentialCore`: write-ahead `credential_operations`, parked values, lease use,
  two-phase rotation with `retire_after`, OIDC eligibility and the trust/secret switching rule,
  host-kind and privileged-credential checks, the credential limit, call timeouts, orphan and
  parked-value sweeps. `resources.py` does the same for non-secret resources.
- `eligibility.py` — A12: `pull` always eligible; `push`/`admin` only with a protected environment
  that no other permission in the repository uses; never org-wide.
- `store.py` — what the core persists and the narrow Protocols it needs from the app (lease, value
  store, GitHub writer, config and tenant resolvers). `sql_store.py` + `models.py` + `codec.py` are
  the Postgres implementation (guard predicate in every write statement); `memory.py` is the
  in-memory one for unit tests and `MODE=mock`.
- Audit hooks (ADR 0032): `SqlCredentialStore(session_factory, observer)` calls a `SaveObserver`
  inside each `save` transaction (opened in the credential's tenant context) with what the rows held
  before, so the audit event commits with the credential state; `CoreDeps.orphans` (an
  `OrphanObserver`) is told about each platform credential the orphan sweep deletes. Both are
  optional; `trustbridge.audit.credentials.CredentialAuditObserver` implements them. The adapters
  package does not import `trustbridge.audit`.
- `fake.py` — a contract-following fake adapter and fake platform for NG5 and the core tests.

## Rules

- Adapters are stateless and never call GitHub; the core writes secrets, variables and environments.
- `github_outputs` is a pure function of `(scope, credential)`.
- No secret in a `Change.detail`, an exception message or a log line.
- This package must not import `trustbridge.github`; the GitHub core imports it.
- `CoreDeps.gate` (an `OperationGate`) refuses a new operation, a refresh and an orphan sweep for an
  admin-disabled tenant with `TenantDisabledError`; an operation already in flight still resumes
  (ADR 0033 invariant 19). The one exception: `SqlTenantGate` admits `OperationKind.DELETE` only when
  the call carries a `deletion_id` (`delete_scope(..., deletion_id=)`, set only by the deletion engine's steps) of a
  non-terminal deletion of the same tenant (ADR 0034). `delete_scope(..., remove_outputs=False)` (also only the deletion's platform step) deletes the platform credential and leaves the recorded GitHub outputs for the deletion's GitHub step. The flag belongs to the call: the platform step passes it on every retry, so a delete left open (by an admin delete, or by a failed attempt) finishes with the outputs kept. The reconciler's own resume uses the default, and it never reaches a `deleting` tenant.

## Rotation when a scope switches kind (settles ADR 0029's open issue)

The core never calls `rotate_begin` for an `OidcTrust` scope. When eligibility changes it starts a
`switch` operation at the next generation: a trust that lost eligibility is replaced by a secret and
the trust is revoked as soon as the secret is delivered (`delete_credential` on the old slot); a
secret that became eligible is replaced by a trust and deleted after the `retire_after` grace.
