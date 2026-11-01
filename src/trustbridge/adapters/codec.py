"""JSON encoding of the persisted adapter state (stored in JSONB columns). Holds no secrets."""

from datetime import datetime
from typing import Any, cast

from trustbridge.adapters.interface import (
    CredentialSlot,
    CredentialState,
    Permission,
    ResourceState,
)
from trustbridge.adapters.store import DeliveredOutput, ScopeId


def slot_to_json(slot: CredentialSlot | None) -> dict[str, Any] | None:
    """Encode a slot."""
    if slot is None:
        return None
    return {
        "generation": slot.generation,
        "name": slot.name,
        "ref": dict(slot.ref),
        "delivered": slot.delivered,
        "retire_after": slot.retire_after.isoformat() if slot.retire_after else None,
    }


def slot_from_json(data: dict[str, Any] | None) -> CredentialSlot | None:
    """Decode a slot."""
    if data is None:
        return None
    retire = data.get("retire_after")
    return CredentialSlot(
        generation=int(data["generation"]),
        name=str(data["name"]),
        ref=dict(data.get("ref") or {}),
        delivered=bool(data["delivered"]),
        retire_after=datetime.fromisoformat(retire) if retire else None,
    )


def state_to_json(state: CredentialState) -> dict[str, Any]:
    """Encode a credential state."""
    return {"current": slot_to_json(state.current), "previous": slot_to_json(state.previous)}


def state_from_json(data: dict[str, Any]) -> CredentialState:
    """Decode a credential state."""
    return CredentialState(
        current=slot_from_json(data.get("current")), previous=slot_from_json(data.get("previous"))
    )


def resource_to_json(state: ResourceState) -> dict[str, Any]:
    """Encode a resource state."""
    return {"name": state.name, "ref": dict(state.ref)}


def resource_from_json(data: dict[str, Any]) -> ResourceState:
    """Decode a resource state."""
    return ResourceState(name=str(data["name"]), ref=dict(data.get("ref") or {}))


def scope_to_json(scope: ScopeId) -> dict[str, Any]:
    """Encode a scope identity."""
    return {
        "repository_id": scope.repository_id,
        "repository_owner_id": scope.repository_owner_id,
        "environment": scope.environment,
        "permission": scope.permission,
    }


def scope_from_json(data: dict[str, Any]) -> ScopeId:
    """Decode a scope identity."""
    return ScopeId(
        repository_id=data["repository_id"],
        repository_owner_id=int(data["repository_owner_id"]),
        environment=data["environment"],
        permission=cast("Permission", data["permission"]),
    )


def outputs_to_json(outputs: tuple[DeliveredOutput, ...]) -> list[dict[str, Any]]:
    """Encode the delivered output names."""
    return [
        {
            "kind": o.kind,
            "name": o.name,
            "repository_id": o.repository_id,
            "environment": o.environment,
        }
        for o in outputs
    ]


def outputs_from_json(data: list[dict[str, Any]]) -> tuple[DeliveredOutput, ...]:
    """Decode the delivered output names."""
    return tuple(
        DeliveredOutput(
            kind=d["kind"],
            name=d["name"],
            repository_id=d["repository_id"],
            environment=d["environment"],
        )
        for d in data
    )
