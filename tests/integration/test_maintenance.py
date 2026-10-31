"""Integration test: app.maintenance.purge_archived against the real Postgres stack service."""

from datetime import UTC, datetime, timedelta

from craftainer_crud.models.base import Base
from craftainer_crud.repositories.sqlalchemy import SQLAlchemyRepository
from sqlalchemy import delete

from app.db import session_factory as async_session_factory
from app.maintenance import purge_archived
from app.models.hero import Hero


async def test_purge_archived_deletes_only_rows_past_older_than() -> None:
    """Only archived rows at or before `older_than` are deleted; everything else is untouched.

    Runs inside one uncommitted session, same isolation as tests/integration/repositories/
    test_sqlalchemy.py -- closing the session without committing rolls back everything
    written here.

    The shared dev database can already hold archived rows (e.g. left behind by an
    e2e dev-leg run), which purge_archived would count too. They're deleted first,
    with the same model discovery purge_archived uses, inside this same session --
    so they're restored by the rollback along with everything else.
    """
    async with async_session_factory() as session:
        for mapper in Base.registry.mappers:
            model = mapper.class_
            if hasattr(model, "archived_at"):
                await session.execute(delete(model).where(model.archived_at.is_not(None)))

        repository = SQLAlchemyRepository(session, Hero)

        old_and_archived = await repository.create(
            {"name": "Ancient Hero", "powers": ["Rust"], "owner_id": "tester"}
        )
        recently_archived = await repository.create(
            {"name": "Recent Hero", "powers": ["Rust"], "owner_id": "tester"}
        )
        never_archived = await repository.create(
            {"name": "Active Hero", "powers": ["Rust"], "owner_id": "tester"}
        )

        assert await repository.delete(old_and_archived.id) is True
        assert await repository.delete(recently_archived.id) is True

        now = datetime.now(UTC).replace(tzinfo=None)
        # Backdate old_and_archived's own archived_at directly -- purge_archived only
        # cares about the column value, not when delete() was actually called.
        old_and_archived.archived_at = now - timedelta(days=30)
        await session.flush()

        deleted = await purge_archived(session, older_than=now - timedelta(days=1))
        assert deleted == 1

        assert await session.get(Hero, old_and_archived.id) is None
        assert await session.get(Hero, recently_archived.id) is not None
        assert await session.get(Hero, never_archived.id) is not None
