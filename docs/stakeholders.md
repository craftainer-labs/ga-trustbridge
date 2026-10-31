# Stakeholders

Who has a stake in this app, what they care about, and how much say
they have over its direction. Referenced from `frs/` and `nfrs/`
requirements' `Source` sections.

| Name / role | Interest | Influence | Notes |
| --- | --- | --- | --- |
| API consumers | Stable, predictable CRUD/versioning/error behavior across JSON, XML, and HTML | Medium | Includes both current (v2) and legacy (v1) integrators. |
| Legacy API consumers | v1 endpoints keep working, with clear deprecation notice before removal | Medium | Depend on [FR-0005](frs/FR-0005-hero-v1-deprecated-compat.md), [NFR-0002](nfrs/NFR-0002-deprecation-sunset-headers.md). |
| Operators / SRE | Health checks, structured logs, automatic migrations, config-from-env | High | Run and monitor the deployed service. |
| Security / compliance | Auth enforcement, RBAC, error redaction, secrets handling | High | Drives [0003-auth-strategy-and-federated-backends](../adrs/0003-auth-strategy-and-federated-backends.md). |
| Platform / infrastructure architects | Stateless, regionally-scalable auth; federated IdP topology | High | See [NFR-0013](nfrs/NFR-0013-stateless-token-validation-scalability.md). |
| Product owner | Feature scope and role matrix for the example Hero resource | High | Owns decisions like [FR-0015](frs/FR-0015-hero-role-matrix.md). |
| Developers maintaining the template | Layering, extensibility, low-cost pattern for new resources/versions | High | The template's primary audience. |
| QA / CI | Coverage gate, three-tier test suite | Medium | See [NFR-0020](nfrs/NFR-0020-test-coverage-gate.md), [NFR-0021](nfrs/NFR-0021-three-tier-test-suite.md). |
| Release engineering | Release smoke-test gate against real dependencies | Medium | See [NFR-0010](nfrs/NFR-0010-release-smoke-test-gate.md). |

## Do

- Add a row as soon as a new requirement's `Source` would otherwise
  need to describe a stakeholder inline.
- Keep "Interest" specific enough that a reader can guess which
  requirements trace back to this stakeholder without opening each one.

## Don't

- Name an individual by their personal name if their role is what
  matters to the requirement — prefer the role (e.g. "on-call SRE"),
  unless a specific person's involvement is itself relevant context.
