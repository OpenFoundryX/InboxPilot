"""Categorization API — the user's taxonomy, rules, and classifier settings.

Backs the Categorization page. The General tab reads and edits the six built-in
categories; the Advanced tab adds custom categories, deterministic rules, and
tuning knobs.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from api.deps import DbSession
from models.categorization import CategorizationSettings, EmailCategory
from models.users import User
from schemas.categorization import (
    CategoryRead,
    CategoryUpdate,
    SettingsRead,
    SettingsUpdate,
)
from services.auth.dependencies import get_current_user
from services.categorization.store import (
    get_category,
    get_or_create_categories,
    get_or_create_settings,
)

router = APIRouter(prefix="/categorization", tags=["categorization"])

CurrentUser = Annotated[User, Depends(get_current_user)]


@router.get("/categories", response_model=list[CategoryRead])
async def list_categories(user: CurrentUser, db: DbSession) -> list[EmailCategory]:
    """The user's taxonomy in display order; seeds the built-ins on first call."""
    return await get_or_create_categories(db, user.id)


@router.patch("/categories/{key}", response_model=CategoryRead)
async def update_category(
    key: str, payload: CategoryUpdate, user: CurrentUser, db: DbSession
) -> EmailCategory:
    # Seed first: a user who PATCHes before ever listing still has a taxonomy.
    await get_or_create_categories(db, user.id)

    category = await get_category(db, user.id, key)
    if category is None:
        raise HTTPException(404, f"no category with key {key!r}")

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(category, field, value)
    return category


@router.get("/settings", response_model=SettingsRead)
async def get_settings(user: CurrentUser, db: DbSession) -> CategorizationSettings:
    return await get_or_create_settings(db, user.id)


@router.put("/settings", response_model=SettingsRead)
async def update_settings(
    payload: SettingsUpdate, user: CurrentUser, db: DbSession
) -> CategorizationSettings:
    row = await get_or_create_settings(db, user.id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, field, value)
    return row
