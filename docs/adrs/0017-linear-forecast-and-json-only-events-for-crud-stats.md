# 0017. Naive OLS forecasting, not a trained model, for the generic `/predict` route; SSE `/events` payloads stay JSON even under the XML router

## Status

Accepted

## Context

The generic CRUD interface (`docs/adrs/0001-mvc-layering-with-a-generic-
crud-interface.md`) gained two opt-in routes, `GET <prefix>/stats` and
`GET <prefix>/predict`, built generically off a resource's existing
view/model the same way `archivable`/`draft_schema`/`revisions`/`events`
are each one opt-in flag on `build_json_router` — see the (now-removed)
`docs/plans/2026-09-crud-stats-and-predictions.md`. Two decisions from
that work are significant and reversible-at-cost enough to record here
rather than only in `controllers/README.md`.

**Forecasting approach.** `/predict` needed some way to project a
resource's time-bucketed series (record count, or a numeric field's
sum) forward by N buckets. A trained-per-resource ML model was one
option; this template otherwise has no ML dependency, no model
storage/retraining/versioning story, and no infrastructure for either.

**XML transport for `/events`.** Bringing `build_xml_router` to parity
with `build_json_router` (the other half of the same plan) raised the
question of what `GET <prefix>/events`'s Server-Sent Events stream
should emit under the XML router — its payloads have always been JSON
dicts (`app.interfaces.base.EventSink`/`EventSource`, per
`interfaces/README.md`), predating this parity work and predating XML
router support entirely.

## Decision

**Forecasting: plain OLS linear regression, stdlib only, explicitly
labeled.** `app.controllers.crud_stats.forecast()` fits an ordinary-
least-squares line over `(bucket_index, value)` pairs using only
`statistics`/basic arithmetic — no numpy, no scikit-learn, no model
artifact. It raises a typed error (surfaced as 422) when fewer than two
buckets of history exist, and every `/predict` response names its
method literally as `"linear_regression"` (`views.stats.PredictionView`)
so a client can never mistake the output for a fitted model's forecast.
This is deliberately naive: a straight-line trend, recomputed from
scratch on every request, with no training, no persistence, and no
per-resource tuning.

**`/events` stays a JSON-payload SSE stream, even under the XML
router.** `build_xml_router`'s `/events` route emits the same JSON
`data:` frames as `build_json_router`'s — it does not run event payloads
through `xml_codec.to_xml`. SSE's `data:` line is a transport envelope,
not a resource representation, and `to_xml` takes a `BaseModel` while
event payloads are plain dicts by design (`interfaces/README.md`'s
`EventSink`/`EventSource` paragraph) — this is the one route under the
XML router that isn't itself XML.

## Consequences

**Forecasting.** Adding `/predict` costs no new dependency and no new
operational surface (nothing to train, store, retrain, or version) —
consistent with this template's low-dependency style. The trade-off is
forecast quality: a straight-line OLS projection cannot capture
seasonality, non-linear trends, or regime changes, and says nothing
about confidence/uncertainty. This is acceptable because `/predict` is
explicit about what it is (`"linear_regression"` in every response,
documented in `controllers/README.md` and `app/README.md`) rather than
presented as a forecasting guarantee. A resource that needs real
forecasting is expected to build that separately and isn't blocked by
this route existing.

**Events.** A client consuming `<prefix>/events` gets the same JSON
event frames regardless of whether it reached the resource through the
JSON or XML router — one less thing for a subscriber to branch on, and
no new dict→XML encoding path for `xml_codec.py` to support (which
would have meant teaching the shared codec a nesting rule every other
resource would then have to reason about too, since `to_xml` is
otherwise flat-model-only). The cost is a documented inconsistency: the
XML router is not *purely* XML end-to-end — one route under it emits
JSON — and this has to stay called out explicitly in
`controllers/README.md` and the route's own OpenAPI docs so it isn't
mistaken for an oversight. If XML-encoded event payloads are ever
actually wanted, this ADR would need to be revisited (superseded, not
edited in place, per `docs/adrs/README.md`).
