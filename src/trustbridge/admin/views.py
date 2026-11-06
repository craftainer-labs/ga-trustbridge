"""What the credential, resource and operation reads may show of their JSONB and text columns.

The adapters persist these columns as names and platform references only (`adapters/codec.py`),
but a column is open-ended, so a read never echoes one as stored. Each shape has an explicit
allow-list of keys; an unknown key, or a value of an unexpected type, is dropped, never passed
through. Free text (`error`, `manual_action`, reference values) goes through the audit log's
`scrub_text`, so it is shown no more openly than it is logged (ADR 0032, ADR 0007).
"""

from collections.abc import Mapping
from typing import Any

from trustbridge.audit.redact import scrub_text, truncate

_SECRET_WORDS = ("token", "password", "passwd", "secret", "authorization", "credential", "key")
_REF_KEY_MAX = 32


def _text(value: object) -> str | None:
    return scrub_text(value) if isinstance(value, str) else None


def _name(value: object) -> str | None:
    """A platform-side name: control characters dropped and bounded, not pattern-masked (a
    deterministic name embeds the tenant id, which the opaque-run mask would wrongly hide)."""
    if not isinstance(value, str):
        return None
    return truncate("".join(c if c.isprintable() else " " for c in value))


def _number(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _flag(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _ref(value: object) -> dict[str, str]:
    """Platform reference pairs (`{"id": ...}`): short keys that do not name a secret."""
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or len(key) > _REF_KEY_MAX or not key.isidentifier():
            continue
        if any(word in key.lower() for word in _SECRET_WORDS):
            continue
        shown = _text(item)
        if shown is not None:
            out[key] = shown
    return out


def _slot(value: object) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {
        "generation": _number(value.get("generation")),
        "name": _name(value.get("name")),
        "ref": _ref(value.get("ref")),
        "delivered": _flag(value.get("delivered")),
        "retire_after": _text(value.get("retire_after")),
    }


def credential_state(value: object) -> dict[str, Any]:
    """`{"current": slot | None, "previous": slot | None}`; nothing else is shown."""
    data = value if isinstance(value, Mapping) else {}
    return {"current": _slot(data.get("current")), "previous": _slot(data.get("previous"))}


def optional_credential_state(value: object) -> dict[str, Any] | None:
    """`credential_state`, or None when there is no state (an operation on a new credential)."""
    return credential_state(value) if isinstance(value, Mapping) else None


def scope(value: object) -> dict[str, Any]:
    """The scope identity: repository, owner, environment and permission."""
    data = value if isinstance(value, Mapping) else {}
    return {
        "repository_id": _number(data.get("repository_id")),
        "repository_owner_id": _number(data.get("repository_owner_id")),
        "environment": _text(data.get("environment")),
        "permission": _text(data.get("permission")),
    }


def outputs(value: object) -> list[dict[str, Any]]:
    """The delivered outputs by kind, name, repository and environment."""
    if not isinstance(value, list):
        return []
    return [
        {
            "kind": _text(item.get("kind")),
            "name": _name(item.get("name")),
            "repository_id": _number(item.get("repository_id")),
            "environment": _text(item.get("environment")),
        }
        for item in value
        if isinstance(item, Mapping)
    ]


def resource_state(value: object) -> dict[str, Any]:
    """A resource's platform name and reference."""
    data = value if isinstance(value, Mapping) else {}
    return {"name": _name(data.get("name")), "ref": _ref(data.get("ref"))}


def free_text(value: str | None) -> str | None:
    """An error or manual-action string, scrubbed and bounded."""
    return None if value is None else scrub_text(value)
