# signer/

Pluggable GitHub App key signers ([ADR 0030](../../../docs/adrs/0030-key-signer-and-github-hosts.md)).

- `base.py` — `AppKeySigner` (sign-only) and `KeyImporter` (an operation of a signer
  implementation, outside the sign-only protocol, used by the manifest flow).
- `config.py` — the `SIGNERS` map, discriminated on `kind`; credentials are file references only.
- `file.py` — `file` kind (dev only; 0600 enforced; refused when `mode=production`).
- `vault.py` — `vault-transit` kind: `POST /v1/<mount>/sign/<key>/sha2-256` with
  `signature_algorithm=pkcs1v15`, Kubernetes/AppRole/token-file auth, BYOK import.
- `factory.py` — builds signers from configuration without reading a key or calling out.

Cloud KMS kinds (`aws-kms`, `gcp-kms`, `azure-key-vault`) come after the first release.
