"""Pydantic view models for the current (v2) Hero resource."""

from typing import Annotated

from craftainer_crud.views.base import IXDTFDatetime, ORMView
from pydantic import Field

Power = Annotated[str, Field(min_length=1, max_length=200)]


class HeroV2Base(ORMView):
    """Fields shared by every Hero view."""

    name: str = Field(min_length=1, max_length=200)
    powers: Annotated[list[Power], Field(min_length=1)]
    power_level: int | None = None


class HeroV2Create(HeroV2Base):
    """Fields accepted when creating a Hero."""


class HeroV2Update(ORMView):
    """Fields accepted when partially updating a Hero -- all optional.

    Also the shape a draft create/publish body validates against (see
    craftainer_crud.controllers.crud_router's `/draft` route and app/README.md's "Example
    CRUD resource: Hero") -- a draft is just a record accepted through this same
    all-optional shape, with `id` and every omitted field taking its column
    default.

    `is_locked`/`publish_at`/`unpublish_at` are the lifecycle fields accepted here
    (unlike `is_draft`/`archived_at`, each set exclusively through its own
    dedicated action) -- see craftainer_crud.models.mixins.Lockable/Schedulable: lock/unlock
    and scheduling both reuse this normal PATCH route rather than getting
    dedicated routes of their own.
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    powers: Annotated[list[Power], Field(min_length=1)] | None = None
    power_level: int | None = None
    is_locked: bool | None = None
    publish_at: IXDTFDatetime | None = None
    unpublish_at: IXDTFDatetime | None = None


class HeroV2(ORMView):
    """A Hero as returned by the API, including its assigned id, owner, and lifecycle state.

    `name`/`powers` are optional here (unlike HeroV2Base/HeroV2Create) since a
    draft Hero -- see craftainer_crud.models.mixins.Draftable -- may have either or both
    still unset. `is_draft`/`archived_at`/`publish_at`/`unpublish_at`/`is_locked`
    are every record-lifecycle mixin Hero demonstrates (see craftainer_crud.models.mixins);
    all are read-only here -- `is_draft`/`archived_at` are set exclusively through
    their own dedicated actions (`/draft`+`/publish`, `DELETE`+`/restore`), while
    `is_locked`/`publish_at`/`unpublish_at` are set through the normal `PATCH`
    route instead (see HeroV2Update) -- none are accepted on HeroV2Create.
    """

    id: int
    name: str | None = Field(default=None, min_length=1, max_length=200)
    powers: Annotated[list[Power], Field(min_length=1)] | None = None
    power_level: int | None = None
    owner_id: str
    is_draft: bool
    archived_at: IXDTFDatetime | None
    publish_at: IXDTFDatetime | None
    unpublish_at: IXDTFDatetime | None
    is_locked: bool
    created_at: IXDTFDatetime
    updated_at: IXDTFDatetime
