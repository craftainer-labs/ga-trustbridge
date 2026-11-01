# trustbridge/webhooks/

`POST /webhooks/github/{host}`: one route per configured GitHub host, so the verifier knows which
webhook secret to use before the payload is parsed, and `POST /webhooks/argocd/{tenant_id}` for
Argo CD notifications
([ADR 0028](../../../docs/adrs/0028-trustbridge-architecture-and-instance-layout.md), "Request
path and processing" and "Abuse limits on the webhook route").

- `ingress.py`: the route, in this order: limiter dependency; host lookup (unknown host 404);
  the host's webhook secret (`WebhookSecretResolver.secret_for`: an `unregistered` host or no
  cached or readable secret is 503, with the body unread); `Content-Length` over `WEBHOOK_MAX_BODY_BYTES`
  is 413 before any read, a streamed body that crosses the cap is aborted; the signature
  (`X-Hub-Signature-256`) is verified in constant time before anything is parsed or stored (a
  mismatch re-reads the secret once, for a rotation); the
  delivery row is committed (`webhook_deliveries`) and only then 202 is returned. A repeated
  delivery id returns 202 without a second row. An `unavailable` host is handled like any other
  (its rows wait in the outbox). The JSON body is never parsed here, and no GitHub or adapter call
  happens on the request path. After the response a best-effort relay adds the delivery id to the
  stream; Valkey is never required.
- `argocd.py`: the Argo CD notification route. The tenant's `notification_token` (in its `argocd`
  connection data) is compared with the `Authorization: Bearer` header in constant time
  (`verify_static_header`); unknown tenant, missing and wrong token are all 401. The body only
  names the application: `ArgoCdDeploymentHandler.handle` re-reads the state through the
  `DeploymentStateSource` (NG5d) before GitHub is written. 503 without a state source or when the
  host is not `ready`.
- `limiter.py`: the Valkey-backed fixed-window limiter per source address (default 300 requests
  per 60 s, /64 for IPv6, 50 ms total budget, fail open, exempt CIDRs). It reads only
  `request.client.host`; `trustbridge.main` installs uvicorn's `ProxyHeadersMiddleware` with
  `TRUSTED_PROXIES`, and the api must run with `--no-proxy-headers`.

The webhook secret comes from the secret store (`worker/secrets.py`) on first use and is cached in
process memory by `github/webhook_secrets.py`, so a store outage after one successful read does not stop ingest. A process with
no cached secret for a host answers 503 for it (the failed delivery is redelivered by the
reconciler once the host is `ready`).

## Don't

- Read the request body, or parse it, before the limiter and the signature check have passed.
- Call GitHub, an adapter or Valkey (other than the limiter and the after-response relay) from
  the request path.
- Parse `X-Forwarded-For` here: the proxy middleware owns that.
