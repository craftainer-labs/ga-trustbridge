"""Shared helper for tests/unit/crud_1/heroes: an owner-aware get_hero_crud override.

Hero.owner_id has no default (see app.models.hero) and craftainer_crud.interfaces.base.
CRUDInterface.create only stamps it when an OwnerScope is set -- every test in
this package that overrides get_hero_crud needs one, mirroring the real
get_hero_crud's own OwnerScope("owner_id", claims["sub"], read_scoped=False)
(see app.crud_1.heroes.heroes_v2) rather than a bare, unscoped CRUDInterface.
"""

from typing import Annotated, Any

from craftainer_crud.interfaces.base import CRUDInterface, OwnerScope
from craftainer_crud.repositories.memory import InMemoryRepository
from fastapi import Depends

from app.models.hero import Hero as HeroModel
from app.oidc import get_current_claims
from app.views.hero_v2 import HeroV2


def override_hero_crud(
    repository: InMemoryRepository[HeroModel],
) -> Any:  # noqa: ANN401 -- a FastAPI dependency-override callable, shape checked by the framework
    """Build a get_hero_crud override sharing `repository`, with real claims->OwnerScope wiring.

    Only the storage is faked (a shared InMemoryRepository instead of a real
    SQLAlchemyRepository/session) -- the owner is still resolved from whatever
    claims the request actually carries, the same as the real get_hero_crud.
    """

    def _build(
        claims: Annotated[dict[str, Any], Depends(get_current_claims)],
    ) -> CRUDInterface[HeroV2, HeroModel]:
        return CRUDInterface(
            schema=HeroV2,
            repository=repository,
            owner=OwnerScope("owner_id", claims["sub"], read_scoped=False),
        )

    return _build
