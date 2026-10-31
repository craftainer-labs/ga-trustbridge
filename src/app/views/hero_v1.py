"""Deprecated v1 Hero view (single superpower) and its converters to/from v2."""

from craftainer_crud.views.base import IXDTFDatetime, ORMView
from pydantic import Field

from app.views.hero_v2 import HeroV2, HeroV2Create, HeroV2Update


class HeroV1Base(ORMView):
    """Fields shared by every v1 Hero view."""

    name: str = Field(min_length=1, max_length=200)
    superpower: str = Field(min_length=1, max_length=200)


class HeroV1Create(HeroV1Base):
    """Fields accepted when creating a Hero via the deprecated v1 shape."""


class HeroV1Update(ORMView):
    """Fields accepted when partially updating a Hero via the deprecated v1 shape."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    superpower: str | None = Field(default=None, min_length=1, max_length=200)


class HeroV1(HeroV1Base):
    """A Hero as returned by the deprecated v1 API."""

    id: int
    created_at: IXDTFDatetime
    updated_at: IXDTFDatetime


def hero_v2_to_v1(hero: HeroV2) -> HeroV1:
    """Convert a current (v2) Hero down to the deprecated v1 shape.

    v1 can only represent one power; the first entry in `powers` is treated
    as the primary power. Lossy but deliberate: v1 clients keep working,
    but never see more than one power even if v2 has several.

    v2's `name`/`powers` are optional (a Draftable Hero -- see
    craftainer_crud.models.mixins -- may have either or both still unset); v1 predates
    draft and has no way to represent "unset" (`name`/`superpower` are both
    required, non-empty strings), so a still-draft field falls back to a
    fixed placeholder rather than crashing a v1 client that lists a hero it
    doesn't know is a draft.
    """
    return HeroV1(
        id=hero.id,
        name=hero.name or "(draft)",
        superpower=(hero.powers or ["(draft)"])[0],
        created_at=hero.created_at,
        updated_at=hero.updated_at,
    )


def hero_v1_create_to_v2(payload: HeroV1Create) -> HeroV2Create:
    """Convert a v1 create payload up to the current (v2) shape."""
    return HeroV2Create(name=payload.name, powers=[payload.superpower])


def hero_v1_update_to_v2(payload: HeroV1Update) -> HeroV2Update:
    """Convert a v1 update payload up to the current (v2) shape.

    Only maps `superpower` -> `powers` when it was actually supplied -- an
    unset v1 field must stay unset in v2, not overwrite existing powers
    with a single-element list.
    """
    data = payload.model_dump(exclude_unset=True)
    if "superpower" in data:
        data["powers"] = [data.pop("superpower")]
    return HeroV2Update.model_validate(data)
