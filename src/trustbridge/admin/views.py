"""What the credential, resource and operation reads may show of their JSONB and text columns.

The adapters persist these columns as names and platform references only (`adapters/codec.py`),
but a column is open-ended, so a read never echoes one as stored. Each shape has an explicit
allow-list of keys; an unknown key, or a value of an unexpected type, is dropped, never passed
through. Free text (`error`, `manual_action`, reference values) goes through the audit log's
`scrub_text`, so it is shown no more openly than it is logged (ADR 0032, ADR 0007).
"""

import re
from collections.abc import Mapping, Sequence
from typing import Any

from trustbridge.audit.redact import has_secret_word, printable, scrub_text, truncate

_REF_KEY_MAX = 32


def _text(value: object) -> str | None:
    return scrub_text(value) if isinstance(value, str) else None


def _name(value: object) -> str | None:
    """A platform-side name: control characters dropped and bounded, not pattern-masked (a
    deterministic name embeds the tenant id, which the opaque-run mask would wrongly hide)."""
    if not isinstance(value, str):
        return None
    return truncate(printable(value))


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
        if has_secret_word(key):
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


_ERROR_CLASS = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,63}")
_ENTRY_KEY_MAX = 64
_ENTRY_LIST_MAX = 1000
UNINSTALL_HINT = (
    "Uninstall the GitHub App from the organisation: until then its events for this "
    "installation are acknowledged and dropped (a reinstall gets a new installation id)."
)
"""ADR 0034 B2: what the installation's admins still have to do."""


def error_code(last_error: str | None) -> str | None:
    """The short code of a stored failure: a class name or fixed code, never its message.

    `last_error` is `Class: message`, `code: message` or `code` for the engine and
    `operator <actor>: <reason>` for an operator's fail, which is reported as `operator_failed`.
    Text that does not start with an identifier is `error`.
    """
    if not last_error:
        return None
    if last_error.startswith("operator "):
        return "operator_failed"
    head = last_error.split(":", 1)[0].strip()
    return head if _ERROR_CLASS.fullmatch(head) and not has_secret_word(head) else "error"


def _entry_value(value: object) -> str | int | bool | list[int] | None:
    """One value of a `skipped` entry: a scalar, or a bounded list of numbers (repository ids)."""
    if isinstance(value, bool | int):
        return value
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, list):
        numbers = [n for n in value if isinstance(n, int) and not isinstance(n, bool)]
        return numbers[:_ENTRY_LIST_MAX]
    return None


def skipped_entries(value: object) -> list[dict[str, Any]]:
    """The record's `skipped` list with every key and value held to a short, safe shape.

    Keys are short identifiers that name no secret; values are scalars (strings scrubbed) or a
    list of numbers. Anything else is dropped, never passed through.
    """
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        entry: dict[str, Any] = {}
        for key, raw in item.items():
            if (
                not isinstance(key, str)
                or len(key) > _ENTRY_KEY_MAX
                or not key.isidentifier()
                or has_secret_word(key)
            ):
                continue
            shown = _entry_value(raw)
            if shown is not None:
                entry[key] = shown
        out.append(entry)
    return out


def deletion_counts_view(value: object) -> dict[str, int]:
    """The record's `counts`: short category names to non-negative integers; the rest is dropped."""
    if not isinstance(value, Mapping):
        return {}
    return {
        key: number
        for key, number in value.items()
        if isinstance(key, str)
        and len(key) <= _ENTRY_KEY_MAX
        and key.isidentifier()
        and not has_secret_word(key)
        and isinstance(number, int)
        and not isinstance(number, bool)
        and number >= 0
    }
