"""Categorization API — the user's taxonomy, rules, and classifier settings.

Backs the Categorization page. The General tab reads and edits the six built-in
categories; the Advanced tab adds custom categories, deterministic rules, and
tuning knobs.
"""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from api.deps import DbSession
from models.categorization import CategorizationSettings, EmailCategory, default_actions
from models.users import User
from schemas.categorization import (
    CategoryRead,
    CategoryUpdate,
    ReclassifyRequest,
    ReclassifyResponse,
    SettingsRead,
    SettingsUpdate,
)
from services.auth.dependencies import get_current_user
from services.categorization.store import (
    get_category,
    get_or_create_categories,
    get_or_create_settings,
)
from workers.jobs.reclassify import reclassify as reclassify_task

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

    data = payload.model_dump(exclude_unset=True)
    if "actions" in data:
        # exclude_unset recurses into nested models, so a partial actions PATCH
        # (e.g. {"archive": true}) only carries the keys the caller sent. Merge
        # over the stored value (itself defaulted, in case it's ever missing a
        # key) instead of assigning outright, or the omitted keys are dropped
        # from the JSONB column rather than left at their prior value.
        data["actions"] = {**default_actions(), **(category.actions or {}), **data["actions"]}
    for field, value in data.items():
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


@router.post(
    "/reclassify",
    response_model=ReclassifyResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def reclassify(
    payload: ReclassifyRequest, user: CurrentUser, db: DbSession
) -> ReclassifyResponse:
    """Queue a re-run of categorization over recent mail.

    Mail that already carries one of the user's category labels is left alone,
    so this is safe to call repeatedly after taxonomy edits.
    """
    settings_row = await get_or_create_settings(db, user.id)
    if not settings_row.is_enabled:
        raise HTTPException(409, "categorization is disabled; enable it first")

    task = reclassify_task.delay(str(user.id), payload.days, payload.max_results)
    settings_row.last_reclassify_at = datetime.now(UTC)

    return ReclassifyResponse(
        task_id=task.id, days=payload.days, max_results=payload.max_results
    )
