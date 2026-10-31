# NFR-0002. Communicate deprecated-version sunset via standard headers

## Attribute

Compatibility / API communication.

## Description

Every route on a deprecated API version (currently all `/v1/heroes*`
routes — JSON, XML, web-form, and `components.js`) shall emit an
RFC 8594 `Sunset` header (HTTP-date format), a `Deprecation: true`
header, and a `Link` header pointing at the current-version
equivalent path, applied once per deprecated router rather than
per-route. Current-version routes shall carry none of these headers.

## Source

API consumers; developers. Implemented in `src/app/http_headers.py`,
`src/app/controllers/heroes_v1*.py`.

## Verification

Automated tests assert the presence of `Sunset`/`Deprecation`/`Link`
headers on every `/v1` route and their absence on `/v2` routes (e.g.
`test_v2_responses_carry_no_deprecation_headers`).
