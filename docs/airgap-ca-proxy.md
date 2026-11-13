# Air-gapped installs, custom CAs and outbound proxies

All three are chart values (EB-5). By default trustbridge makes no outbound
calls beyond the ones its configuration names: the GitHub hosts, the OIDC
provider, the platform adapters you enable and the services in
`values.yaml`. OpenTelemetry export is off until `otel.endpoint` is set.

## Air-gapped images

Mirror the image (and, if you pull from one, the chart) into your registry,
then point the chart at it:

```yaml
global:
  imageRegistry: registry.internal.example/mirror   # replaces image.registry
  imagePullSecrets:
    - name: mirror-pull
image:
  repository: craftainer-labs/ga-trustbridge
  digest: sha256:<digest of the mirrored image index>
```

The pulled reference is `<imageRegistry>/<repository>@<digest>`. A digest wins
over `image.tag`, so what runs is exactly what you mirrored and verified
(verify the cosign signature and the SBOM before mirroring). Copy the whole
index with `skopeo copy --all` so every architecture arrives. The chart has no
other images: the migrate Job uses the same one. The hook Job needs the pull
secret too; `global.imagePullSecrets` and `imagePullSecrets` apply to it.

Each GitHub host in `trustbridge.githubHosts` must be reachable from the
cluster (a GHES instance on your network works). The manifest flow and
manual registration work on GHES and in air-gapped setups; see
[install.md](install.md).

## Custom CA bundle

Create a ConfigMap with the PEM bundle of every root your backends need
(GitHub host, OIDC provider, Postgres, Valkey, S3, MQTT, Vault):

```bash
kubectl -n trustbridge create configmap tb-ca --from-file=ca-bundle.crt=./ca.pem
```

```yaml
ca:
  existingConfigMap: tb-ca
  key: ca-bundle.crt        # default
  mountPath: /etc/trustbridge/ca
```

The chart mounts the file read-only and sets `SSL_CERT_FILE`,
`REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `AWS_CA_BUNDLE` and `PGSSLROOTCERT`
to it. Different libraries read different variables: httpx, redis-py and
aiomqtt read `SSL_CERT_FILE`; boto3 reads `AWS_CA_BUNDLE`; asyncpg reads
`PGSSLROOTCERT` for `verify-ca` and `verify-full`. The bundle **replaces** the
image's system roots for these libraries, so include public roots too if a
backend uses one. Only an existing ConfigMap is supported (the migrate hook
runs before the chart's own resources exist). Update the ConfigMap and
restart the pods (`kubectl rollout restart`) to rotate it.

A GitHub host or a Vault signer with its own CA names a file in its JSON
(`ca_bundle`); mount that file with `extraVolumes` and `extraVolumeMounts`.

## Outbound proxy

```yaml
proxy:
  httpProxy: http://proxy.internal.example:3128
  httpsProxy: http://proxy.internal.example:3128
  noProxy: .svc,.cluster.local,10.0.0.0/8,idp.internal.example
```

The values set `HTTP_PROXY`, `HTTPS_PROXY` and `NO_PROXY`, and the lowercase
forms, in every pod including the migrate Job. If the proxy URL carries
credentials, keep it in a Secret instead:

```yaml
proxy:
  existingSecret: tb-proxy
  httpProxyKey: http-proxy
  httpsProxyKey: https-proxy
```

A key name set here replaces the plain value for that variable.

- The proxy variables apply to HTTP clients (httpx: GitHub, the OIDC
  provider, adapters). Postgres, Valkey, MQTT and S3 clients connect
  directly, so list them in the NetworkPolicy egress rules or reach them
  without a proxy; they are not controlled by `NO_PROXY`.
- Put the cluster's service domains and every internal backend in `noProxy`.
- Inbound, the proxy that terminates TLS in front of the api must send
  `X-Forwarded-For` and be listed in `trustbridge.trustedProxies`
  (see [install.md](install.md)).

## NetworkPolicy

```yaml
networkPolicy:
  enabled: true
  apiIngressFrom:
    - namespaceSelector:
        matchLabels: {kubernetes.io/metadata.name: ingress-nginx}
  egress:
    enabled: true
    rules:
      - to: [{namespaceSelector: {}}]   # DNS and in-cluster backends: narrow this
```

With `egress.enabled: true` an empty `rules` list denies all egress, so
write the rules for DNS, Postgres, Valkey, the OIDC provider, S3, MQTT,
GitHub and your proxy. The worker and reconciler accept no inbound traffic.
