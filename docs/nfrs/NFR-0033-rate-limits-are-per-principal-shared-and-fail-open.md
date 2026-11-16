# NFR-0033. Admin rate limits are per principal, shared across replicas, and fail open

## Status

Accepted. Implemented. The default numbers are starting points to be measured against the UI.

## Attribute

Availability, security (abuse resistance), scalability.

## Description

- **Buckets and defaults** (fixed window, `ADMIN_RATE_LIMIT_*`, window 60 s): reads 600 per
  principal, mutations 60, exports 6, and 30 per source address for unauthenticated requests.
  Reads, mutations and exports count in separate buckets. The key is the authenticated principal
  `(iss, sub)` once known and the source address before; a 401, or a request to a route that does
  not exist, counts against the address, and a 403 or 404 for an authenticated principal against
  that principal's bucket for the route's kind (the limit runs before the role check), so callers
  behind one address do not use up each other's budget. A scanner hitting unknown paths can
  exhaust the address bucket; behind the BFF the address is the BFF's unless `TRUSTED_PROXIES`
  lists it. Configured exempt addresses are never limited.
- **Over the limit.** The response is a 429 problem document with `Retry-After` set to the
  whole seconds left in the window, at least 1.
- **Shared across replicas.** The counters live in Valkey under the prefix `tb:rl:admin:`, so
  two api replicas sharing one Valkey share each principal's count. An in-memory counter is used
  where no Valkey is configured, and evicts expired keys.
- **Fail open, visibly.** When Valkey errors or does not answer within
  `ADMIN_RATE_LIMIT_TIMEOUT_MS` (default 50), the request is served and a fail-open metric
  increments; the limiter never takes the admin API down. The same holds for the webhook route,
  where ingest survives through the outbox.
- **Denial audit rows are throttled by their own limiter** (`AUDIT_DENIED_*`: a burst of 5 per
  subject and reason per 60 s window, then counted and summarised) so a denied caller cannot
  flood the audit table; see [NFR-0031](NFR-0031-audit-log-is-append-only-complete-and-exportable.md).

## Source

Enterprise baseline row EB-11 (OpenAPI with rate limiting and `Retry-After`), with EB-6 (HA:
stateless replicas, so the count is shared) of the plan `docs/plans/2026-10-craftainer-labs-apps.md`;
user decision 5 in [ADR 0033](../adrs/0033-admin-api-v1-rbac-and-limits.md) and the application
limiter of [ADR 0028](../adrs/0028-trustbridge-architecture-and-instance-layout.md) (user decision
2026-11-01, item 10(b): Valkey-backed, per source address, fail open). Stakeholders: Operators / SRE, Security
/ compliance. Related: [NFR-0024](NFR-0024-api-latency-and-throughput-targets.md).

## Verification

Unit tests run in the Checks workflow's prek job (pytest, 100% coverage gate).

- `tests/trustbridge/admin/test_limits_unit.py`:
  `test_reads_over_the_bucket_get_429_with_retry_after`, `test_the_key_is_the_principal_not_the_address`,
  `test_rejected_requests_count_against_the_address_bucket`,
  `test_an_authenticated_403_counts_against_the_principal_not_the_address`,
  `test_an_authenticated_404_counts_against_the_principal`,
  `test_401s_from_one_address_still_hit_the_address_limit`, `test_an_exempt_address_is_never_limited`,
  `test_replicas_sharing_valkey_share_the_count`, `test_valkey_down_serves_the_request`
  (also asserts the fail-open metric), `test_disabled_limits_never_reject`.
- Separate mutation and export buckets: `test_mutations_count_against_their_own_bucket` and
  `test_a_denied_tenant_request_consumes_the_principals_mutation_bucket` in
  `tests/trustbridge/admin/test_guards_unit.py`; `test_exports_have_their_own_small_bucket` in
  `tests/trustbridge/admin/test_routes_unit.py`.
- The limiter core (`tests/trustbridge/test_ratelimit.py`):
  `test_keys_are_counted_under_the_prefix_and_separately`, `test_failures_without_metrics_still_fail_open`,
  `test_the_in_memory_counter_evicts_expired_keys`,
  `test_an_expired_key_restarts_from_one_even_before_the_sweep`.
- Denial throttle: `tests/trustbridge/audit/test_throttle_unit.py`.

Known gaps: no test runs two replicas against a real Valkey (the shared count is tested through a
shared counter object); the 600/60/30/6 numbers are not validated by a load test, and no
benchmark covers the limiter's added latency against the 50 ms budget.
