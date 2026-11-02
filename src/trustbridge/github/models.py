"""The host registration table (ADR 0030, open issue settled here).

The operator's `GITHUB_HOSTS` entry is read-only configuration. When a host is configured
without an App, the manifest flow (or the manual fallback) stores the result here, readable by
every process (api, worker, reconciler), and each process overlays it on the entry at its next
recheck, so a registration is picked up without a restart. The row holds the App id, a secret
reference and the slug; the PEM goes to the signer and the webhook secret to the secret store.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime

from craftainer_crud.models.base import Base
from sqlalchemy import BigInteger, DateTime, String, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column


class HostRegistrationRow(Base):
    """One registered host: App id and the reference to its webhook secret."""

    __tablename__ = "github_host_registrations"

    host_name: Mapped[str] = mapped_column(String, primary_key=True)
    app_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    webhook_secret_ref: Mapped[str] = mapped_column(String, nullable=False)
    app_slug: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


@dataclass(frozen=True, slots=True)
class Registration:
    """The result of registering a host's App."""

    app_id: int
    webhook_secret_ref: str
    app_slug: str | None = None


class SqlRegistrationStore:
    """`RegistrationStore` over `github_host_registrations`."""

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        """Use `session_factory` for a short-lived session per call."""
        self._sf = session_factory

    async def load_all(self) -> Mapping[str, Registration]:
        """Return every registration by host name."""
        async with self._sf() as session:
            rows = await session.scalars(select(HostRegistrationRow))
            return {
                r.host_name: Registration(r.app_id, r.webhook_secret_ref, r.app_slug) for r in rows
            }

    async def save(self, host_name: str, registration: Registration) -> None:
        """Insert or replace a host's registration."""
        stmt = insert(HostRegistrationRow).values(
            host_name=host_name,
            app_id=registration.app_id,
            webhook_secret_ref=registration.webhook_secret_ref,
            app_slug=registration.app_slug,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["host_name"],
            set_={
                "app_id": stmt.excluded.app_id,
                "webhook_secret_ref": stmt.excluded.webhook_secret_ref,
                "app_slug": stmt.excluded.app_slug,
            },
        )
        async with self._sf() as session, session.begin():
            await session.execute(stmt)
