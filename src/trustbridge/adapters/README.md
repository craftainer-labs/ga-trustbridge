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
- `fake.py` — a contract-following fake adapter and fake platform for NG5 and the core tests.

## Rules

- Adapters are stateless and never call GitHub; the core writes secrets, variables and environments.
- `github_outputs` is a pure function of `(scope, credential)`.
- No secret in a `Change.detail`, an exception message or a log line.
- This package must not import `trustbridge.github`; the GitHub core imports it.

## Rotation when a scope switches kind (settles ADR 0029's open issue)

The core never calls `rotate_begin` for an `OidcTrust` scope. When eligibility changes it starts a
`switch` operation at the next generation: a trust that lost eligibility is replaced by a secret and
the trust is revoked as soon as the secret is delivered (`delete_credential` on the old slot); a
secret that became eligible is replaced by a trust and deleted after the `retire_after` grace.
