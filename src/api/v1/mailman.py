"""Mailman (batched delivery) settings API.

Batching is enforced by a server-side Gmail filter that skips the inbox for
non-VIP mail (so no arrival notification fires); a scheduled worker releases the
held mail back to the inbox at the user's delivery slots.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool

from api.deps import DbSession
from integrations.composio import gmail
from models.mailman import (
    MODE_CUSTOM_DAILY,
    MODE_INTERVAL,
    MODE_TIMES,
    MailmanSettings,
)
from models.users import User
from schemas.email import EmailSummary
from schemas.mailman import (
    MailmanStatus,
    SettingsRead,
    SettingsUpdate,
    VipRead,
    VipUpdate,
)
from services.auth.dependencies import get_current_user
from services.mailman import gmail_ops
from services.mailman.store import get_or_create_settings, get_or_create_vip

router = APIRouter(prefix="/mailman", tags=["mailman"])

CurrentUser = Annotated[User, Depends(get_current_user)]
_MODES = {MODE_INTERVAL, MODE_TIMES, MODE_CUSTOM_DAILY}

# NOTE: holding is done by a Gmail filter the user creates manually ("Skip the
# Inbox" + apply the inboxos-later label), because programmatic filter creation
# needs the gmail.settings.basic scope. The app only releases held mail on
# schedule (that needs just gmail.modify, which the connection already has).


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
    return vip


@router.get("/held", response_model=list[EmailSummary])
async def list_held(user: CurrentUser) -> list[EmailSummary]:
    return await run_in_threadpool(
        gmail.fetch_by_query, str(user.id), f"label:{gmail_ops.HOLD_LABEL_NAME}", 50
    )


@router.get("/status", response_model=MailmanStatus)
async def status(user: CurrentUser, db: DbSession) -> MailmanStatus:
    settings = await get_or_create_settings(db, user.id)
    held = await run_in_threadpool(gmail_ops.held_message_ids, str(user.id))
    return MailmanStatus(is_active=settings.is_active, held_count=len(held))


@router.post("/start", response_model=SettingsRead)
async def start(user: CurrentUser, db: DbSession) -> MailmanSettings:
    """Activate scheduled release. Assumes the user's skip-inbox filter exists."""
    settings = await get_or_create_settings(db, user.id)
    await run_in_threadpool(gmail.ensure_labels, str(user.id))
    settings.is_active = True
    return settings


@router.post("/stop", response_model=SettingsRead)
async def stop(user: CurrentUser, db: DbSession) -> MailmanSettings:
    """Stop scheduled release.

    Note: the user's Gmail filter keeps holding new mail under `inboxos-later`;
    to fully resume normal delivery, remove that filter in Gmail settings.
    """
    settings = await get_or_create_settings(db, user.id)
    settings.is_active = False
    return settings
