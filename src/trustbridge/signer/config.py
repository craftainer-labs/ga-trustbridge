"""The `SIGNERS` setting: named signer configurations, discriminated on `kind` (ADR 0030).

Credentials are file references (`secret_id_file`, `token_file`), never inline values: the
models forbid unknown fields, so an inline `token` or `secret_id` cannot even be expressed, and a
validator additionally rejects PEM blocks and Vault token prefixes in any string value.
"""

import json
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

_FORBIDDEN_FRAGMENTS = ("-----BEGIN", "hvs.", "hvb.")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="after")
    def _no_inline_secrets(self) -> Self:
        for name, value in self.__dict__.items():
            if isinstance(value, str | Path) and str(value).startswith(_FORBIDDEN_FRAGMENTS):
                msg = f"{name}: signer configuration must not contain an inline secret value"
                raise ValueError(msg)
        return self


class FileSignerConfig(_Strict):
    """Reads a PEM from a path. Dev only: refused when `mode=production`."""

    kind: Literal["file"]
    path: Path


class VaultKubernetesAuth(_Strict):
    """Vault Kubernetes auth: logs in with the pod's service-account token."""

    method: Literal["kubernetes"]
    role: str
    jwt_file: Path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
    auth_mount: str = "kubernetes"


class VaultAppRoleAuth(_Strict):
    """Vault AppRole auth: the role id plus a secret id read from a file."""

    method: Literal["approle"]
    role_id: str
    secret_id_file: Path
    auth_mount: str = "approle"


class VaultTokenAuth(_Strict):
    """A Vault token read from a file (for example one rendered by Vault Agent)."""

    method: Literal["token"]
    token_file: Path


VaultAuth = Annotated[
    VaultKubernetesAuth | VaultAppRoleAuth | VaultTokenAuth, Field(discriminator="method")
]


class VaultTransitSignerConfig(_Strict):
    """Signs with a non-exportable Vault Transit RSA key."""

    kind: Literal["vault-transit"]
    addr: str
    key: str
    auth: VaultAuth
    mount: str = "transit"
    namespace: str | None = None
    ca_bundle: Path | None = None

    @model_validator(mode="after")
    def _https(self) -> Self:
        if not self.addr.startswith("https://"):
            msg = "vault-transit addr must be an https URL"
            raise ValueError(msg)
        return self


SignerConfig = Annotated[FileSignerConfig | VaultTransitSignerConfig, Field(discriminator="kind")]

_SIGNERS_ADAPTER: TypeAdapter[dict[str, SignerConfig]] = TypeAdapter(dict[str, SignerConfig])


def parse_signers(raw: str | dict[str, object]) -> dict[str, SignerConfig]:
    """Parse the `SIGNERS` map (JSON text or an already-decoded mapping)."""
    data = json.loads(raw) if isinstance(raw, str) else raw
    return _SIGNERS_ADAPTER.validate_python(data)
