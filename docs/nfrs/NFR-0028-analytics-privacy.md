# NFR-0028. Analytics stays within the CNIL audience-measurement exemption, read strictly

## Status

Accepted

## Attribute

Privacy / compliance, data minimisation.

## Description

Product analytics shall collect only what the CNIL's audience-measurement exemption
permits, and where the CNIL's pages leave room, the stricter reading applies. This is the
engineering reference the template is built against. **It is not a legal opinion**; a
deployment remains responsible for its own lawful basis, notices and any consent it needs.

The checklist (CNIL, "Cookies et autres traceurs: solutions pour les outils de mesure
d'audience" and fiche n°16 on analytics), and how this template meets it:

- **Strictly limited purpose.** Anonymous audience statistics only: the events are
  `hero.created` and `hero.deleted`, counted. No profiling, no cross-site or cross-service
  tracking, no use for advertising, and no combining with other data.
- **No raw IP address.** Backend events are sent with the constant `payload.ip = server`
  (never a client address) and no user agent of the visitor, so Umami stores and derives
  nothing about the client, including geography (`ANALYTICS_GEO=none`). The strict reading
  is adopted here even where a page allows a truncated address.
- **No personal data in events.** Event names are fixed; props are scalars on the
  `ANALYTICS_PROP_KEYS` allowlist (empty by default) and carry counts, never names, ids,
  owners, claims or request headers; `url` is a path with no query or fragment.
- **Cookieless.** No identifier is stored on or read from the visitor's device; this
  service sets no analytics cookie.
- **No cross-day tracking.** Umami runs with `SALT_ROTATION=day`, so any visitor hash
  rotates daily.
- **Limited retention: 25 months.** Data older than 25 months is purged (the CNIL's
  maximum; the strictest figure across its pages). Umami OSS has no retention setting, so
  it is a manual purge (`.devcontainer/stack/umami/purge.sql`), not scheduled by this
  template; a deployment schedules it.
- **Opt-out and signals respected.** `Sec-GPC: 1` and `DNT: 1` stop tracking for the request
  (`ANALYTICS_RESPECT_GPC`, default true), and `ANALYTICS_CONSENT=opt-in` requires an
  explicit consent signal (`Analytics-Consent: 1`) instead of assuming it.
- **No third-party sharing.** Umami is self-hosted in the deployment; the data is not
  passed to a third party for its own purposes.

## Source

Privacy by design for the analytics added in
[0027-product-analytics-with-track](../adrs/0027-product-analytics-with-track.md); the
plan's research (RX2) of the CNIL pages:
<https://www.cnil.fr/fr/cookies-et-autres-traceurs/regles/cookies-solutions-pour-les-outils-de-mesure-daudience>,
<https://www.cnil.fr/en/sheet-ndeg16-use-analytics-your-websites-and-applications>.
Where those pages disagree, the stricter figure or condition is used (no raw IP; 25
months). Builds on [FR-0026](../frs/FR-0026-product-analytics-events.md).

## Verification

- `tests/unit/test_analytics.py` and `tests/unit/crud_1/test_analytics_events.py`: GPC,
  DNT and opt-in drop events; only the allowlisted count is sent, with no name or id; the
  default backend records nothing.
- The library's own tests cover the constant `payload.ip`, path-only `url`, prop
  allowlisting and the Umami payload; the integration/e2e run against the stack's Umami
  checks that events arrive.
- Code review for what tests cannot show: a new event or prop key is a privacy change and
  is reviewed against this list; the retention purge is scheduled by the deployment.
