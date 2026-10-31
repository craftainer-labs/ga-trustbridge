"""Unit test: HeroV2 views validate input and convert from an ORM-shaped object."""

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.views.hero_v2 import HeroV2, HeroV2Create, HeroV2Update

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass
class _FakeORMHero:
    """Stand-in for a SQLAlchemy Hero instance -- just needs matching attributes."""

    id: int
    name: str | None
    powers: list[str] | None
    owner_id: str
    created_at: datetime
    updated_at: datetime
    is_draft: bool = False
    archived_at: datetime | None = None
    publish_at: datetime | None = None
    unpublish_at: datetime | None = None
    is_locked: bool = False


def test_hero_create_accepts_valid_fields() -> None:
    """HeroV2Create accepts a name and powers within the length bounds."""
    hero = HeroV2Create(name="Spider-Man", powers=["Wall-crawling"])
    assert hero.name == "Spider-Man"


def test_hero_create_rejects_empty_name() -> None:
    """HeroV2Create rejects an empty name."""
    with pytest.raises(ValidationError):
        HeroV2Create(name="", powers=["Flight"])


def test_hero_create_rejects_empty_powers() -> None:
    """HeroV2Create rejects an empty powers list."""
    with pytest.raises(ValidationError):
        HeroV2Create(name="Nobody", powers=[])


def test_hero_update_allows_all_fields_omitted() -> None:
    """HeroV2Update accepts an empty payload -- every field is optional."""
    update = HeroV2Update()
    assert update.name is None
    assert update.powers is None


def test_hero_converts_from_orm_instance() -> None:
    """HeroV2.model_validate builds a view straight from an ORM-shaped object."""
    orm_hero = _FakeORMHero(
        id=1,
        name="Batman",
        powers=["Detective skills"],
        owner_id="alice",
        created_at=_NOW,
        updated_at=_NOW,
    )
    hero = HeroV2.model_validate(orm_hero)
    assert hero.id == 1
    assert hero.name == "Batman"


def test_hero_serializes_timestamps_as_ixdtf() -> None:
    """HeroV2's created_at/updated_at serialize as RFC 9557 IXDTF strings."""
    hero = HeroV2(
        id=1,
        name="Batman",
        powers=["Detective skills"],
        owner_id="alice",
        is_draft=False,
        archived_at=None,
        publish_at=None,
        unpublish_at=None,
        is_locked=False,
        created_at=_NOW,
        updated_at=_NOW,
    )
    assert hero.model_dump(mode="json")["created_at"] == "2026-01-01T00:00:00Z[UTC]"
