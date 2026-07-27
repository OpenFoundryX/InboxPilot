"""Categorization API — the user's taxonomy, rules, and classifier settings.

Backs the Categorization page. The General tab reads and edits the six built-in
categories; the Advanced tab adds custom categories, deterministic rules, and
tuning knobs.
"""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.concurrency import run_in_threadpool

from api.deps import DbSession
from integrations.composio import gmail
from models.categorization import CategorizationSettings, EmailCategory, default_actions
from models.users import User
from schemas.categorization import (
    CategoryCreate,
    CategoryRead,
    CategoryUpdate,
    ReclassifyRequest,
    ReclassifyResponse,
    SettingsRead,
    SettingsUpdate,
)
from services.auth.dependencies import get_current_user
from services.categorization.store import (
    delete_category,
    get_category,
    get_or_create_categories,
    get_or_create_settings,
    slugify,
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

    # Stamp before enqueueing: if the broker publish in `.delay()` raises, the
    # request unwinds and the stamp rolls back with it, so the two stay
    # consistent. `task.id` is safe either way — Celery generates it
    # client-side before publishing.
    settings_row.last_reclassify_at = datetime.now(UTC)
    task = reclassify_task.delay(str(user.id), payload.days, payload.max_results)

    return ReclassifyResponse(
        task_id=task.id, days=payload.days, max_results=payload.max_results
    )


@router.post(
    "/categories", response_model=CategoryRead, status_code=status.HTTP_201_CREATED
)
async def create_category(
    payload: CategoryCreate, user: CurrentUser, db: DbSession
) -> EmailCategory:
    """Create a custom category and its Gmail label.

    The Gmail label is created before the row is committed: a category whose
    label does not exist would fail every classification that picked it, so the
    Composio failure has to surface here rather than in a worker.
    """
    existing = await get_or_create_categories(db, user.id)

    gmail_label = payload.display_name.strip().casefold()
    key = slugify(payload.display_name)
    if not key:
        raise HTTPException(422, "display_name must contain at least one letter or digit")
    if gmail_label in gmail.RESERVED_LABEL_NAMES:
        raise HTTPException(422, f"{payload.display_name!r} is a reserved label name")
    if any(c.key == key for c in existing):
        raise HTTPException(422, f"a category with key {key!r} already exists")
    if any(c.gmail_label == gmail_label for c in existing):
        raise HTTPException(422, f"a category already uses the label {gmail_label!r}")
    # Colliding display names shadow each other in the classifier: the model
    # picks a category by display name, so two categories that normalize to
    # the same string would make mail for the shadowed one silently land in
    # the other, permanently. Reject the collision outright rather than let
    # the classifier degrade.
    if any(
        c.display_name.strip().casefold() == payload.display_name.strip().casefold()
        for c in existing
    ):
        raise HTTPException(422, f"a category is already named {payload.display_name.strip()!r}")

    try:
        await run_in_threadpool(gmail.create_label, str(user.id), gmail_label)
    except Exception as exc:
        raise HTTPException(502, f"could not create the Gmail label: {exc}") from exc

    category = EmailCategory(
        user_id=user.id,
        key=key,
        gmail_label=gmail_label,
        display_name=payload.display_name.strip(),
        description=payload.description,
        color_bg=payload.color_bg,
        color_text=payload.color_text,
        is_builtin=False,
        is_enabled=True,
        sort_order=payload.sort_order,
        actions=payload.actions.model_dump(),
    )
    db.add(category)
    await db.flush()
    return category


@router.delete("/categories/{key}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_category(key: str, user: CurrentUser, db: DbSession) -> None:
    """Delete a custom category. Its Gmail label is left in the user's mailbox."""
    await get_or_create_categories(db, user.id)

    category = await get_category(db, user.id, key)
    if category is None:
        raise HTTPException(404, f"no category with key {key!r}")
    if category.is_builtin:
        raise HTTPException(409, "built-in categories cannot be deleted; disable it instead")

    await delete_category(db, user.id, category)
