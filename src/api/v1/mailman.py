"""Mailman (batched delivery) settings API.

Batching is enforced by a server-side Gmail filter that skips the inbox for
non-VIP mail (so no arrival notification fires); a scheduled worker releases the
held mail back to the inbox at the user's delivery slots. The app creates and
maintains that filter automatically (needs the gmail.settings.basic scope).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool

from api.deps import DbSession
from models.mailman import (
    MODE_CUSTOM_DAILY,
    MODE_INTERVAL,
    MODE_TIMES,
    MailmanSettings,
    VipRule,
)
from models.users import User
from schemas.mailman import (
    SettingsRead,
    SettingsUpdate,
    VipRead,
    VipUpdate,
)
from services.auth.dependencies import get_current_user
from services.mailman import filters
from services.mailman.store import get_or_create_settings, get_or_create_vip

router = APIRouter(prefix="/mailman", tags=["mailman"])


CurrentUser = Annotated[User, Depends(get_current_user)]

_MODES = {
    MODE_INTERVAL, 
    MODE_TIMES, 
    MODE_CUSTOM_DAILY
}


async def _apply_hold_filter(settings: MailmanSettings, vip: VipRule) -> None:
    """Sync the live Gmail hold filter to current VIP rules; persist the new id."""
    settings.gmail_filter_id = await run_in_threadpool(
        filters.apply_hold_filter,
        str(settings.user_id),
        domains=vip.domains,
        addresses=vip.addresses,
        keywords=vip.keywords,
        existing_filter_id=settings.gmail_filter_id,
    )


@router.get("/settings", response_model=SettingsRead)
async def get_settings(user: CurrentUser, db: DbSession) -> MailmanSettings:
    return await get_or_create_settings(db, user.id)


@router.put("/settings", response_model=SettingsRead)
async def update_settings(
    payload: SettingsUpdate, user: CurrentUser, db: DbSession
) -> MailmanSettings:
    settings = await get_or_create_settings(db, user.id)
    data = payload.model_dump(exclude_unset=True)
    if "delivery_mode" in data and data["delivery_mode"] not in _MODES:
        raise HTTPException(422, f"delivery_mode must be one of {sorted(_MODES)}")
    for key, value in data.items():
        setattr(settings, key, value)
    return settings


@router.get("/vip", response_model=VipRead)
async def get_vip(user: CurrentUser, db: DbSession):
    return await get_or_create_vip(db, user.id)


@router.put("/vip", response_model=VipRead)
async def update_vip(payload: VipUpdate, user: CurrentUser, db: DbSession):
    vip = await get_or_create_vip(db, user.id)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(vip, key, value)

    settings = await get_or_create_settings(db, user.id)
    if settings.is_active:
        await _apply_hold_filter(settings, vip)

    return vip


@router.post("/start", response_model=SettingsRead)
async def start(user: CurrentUser, db: DbSession) -> MailmanSettings:
    """Activate batching: install the skip-inbox Gmail filter."""

    settings = await get_or_create_settings(db, user.id)
    vip = await get_or_create_vip(db, user.id)
    await _apply_hold_filter(settings, vip)
    settings.is_active = True
    return settings


@router.post("/stop", response_model=SettingsRead)
async def stop(user: CurrentUser, db: DbSession) -> MailmanSettings:
    """Deactivate batching and remove the skip-inbox filter.

    New mail resumes landing in the inbox normally. Already-held mail keeps its
    `inboxos-later` label until the next delivery slot (or a manual release).
    """
    settings = await get_or_create_settings(db, user.id)

    await run_in_threadpool(
        filters.remove_hold_filter, 
        str(user.id), 
        settings.gmail_filter_id
    )

    settings.gmail_filter_id = None
    settings.is_active = False
    return settings
