# tests/keycloak_e2e/

The admin API (`/admin/v1`) called with access tokens a real Keycloak issued, so the claim
shape of the namespaced client roles (`trustbridge:<role>`, `trustbridge:<host>:<org>:<role>`,
ADR 0033) is tested as Keycloak emits it, not as synthetic tokens (NFR-0030).

Run only by `.github/workflows/keycloak-e2e.yml`, which starts the image the devcontainer uses
(same tag and digest) with `realm-trustbridge-e2e.json` and sets `MODE=dev` and
`OIDC_ISSUER_URL` before pytest starts (`app.oidc` reads its settings at import). The directory
is in `pyproject.toml`'s `--ignore`, so a plain `pytest` never collects it, and the workflow
clears `addopts`. Don't run it in the devcontainer: the stack's Keycloak has a different realm.

- `realm-trustbridge-e2e.json` — the four ADR 0033 roles at instance scope and, for the tenants
  `acme` and `other` on `github.com`, at tenant scope, as client roles of `api`; one user per case
  (password = username). Clients: `e2e-cli` (password grant, audience `api`), `e2e-short` (same,
  2 second access token), `e2e-other-audience` (audience `other-service` only).
- The app is the real admin router and the real `get_current_claims` over the fakes of
  `tests/trustbridge/admin/helpers.py`; one tenant read is patched.

## Don't

- Add users or roles the tests don't use; each user is one test case.
