"""Integration routes (API v1) — Gmail via Composio."""

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse

from core.config import settings
from core.logging import get_logger
from integrations.composio import calendar, gmail, triggers
from integrations.composio.triggers import gmail_oauth_callback_url
from models.users import User
from schemas.integrations import (
    CalendarConnect,
    CalendarStatus,
    GmailConnect,
    GmailStatus,
)
from services.auth.dependencies import get_current_user
from workers.jobs.sync_last_7_days import sync_last_7_days

log = get_logger(__name__)

# Composio reports the grant outcome on the return URL; anything else is a
# failed or abandoned authorisation.
_GRANT_OK = {"", "success", "active"}
# Give Composio a moment to finish marking the connection ACTIVE before the
# worker starts calling Gmail with it.
_ONBOARD_DELAY_SECONDS = 5

router = APIRouter(prefix="/integrations", tags=["integrations"])

CurrentUser = Annotated[User, Depends(get_current_user)]


@router.get("/gmail/status", response_model=GmailStatus)
async def gmail_status(user: CurrentUser) -> GmailStatus:
    """Whether Gmail is connected, and whether new mail actually reaches us.

    `connected` without `listening` is the silent-failure state: the grant is
    fine, but no trigger exists, so no mail is being processed at all —
    reconnecting the account re-runs onboarding and reinstalls it.
    """
    connection = await run_in_threadpool(gmail.get_active_connection, str(user.id))
    if not connection:
        return GmailStatus(connected=False, listening=False)

    listening = await run_in_threadpool(triggers.has_gmail_trigger, connection.id)
    return GmailStatus(connected=True, listening=listening)


@router.get("/gmail/connect", response_model=GmailConnect)
async def gmail_connect(user: CurrentUser) -> GmailConnect:
    """Start the Gmail OAuth grant; returns a URL to send the user to.

    Composio returns the browser to our own callback, which starts onboarding
    and then forwards to the in-app connect step.
    """
    return_url = gmail_oauth_callback_url(str(user.id))
    redirect_url = await run_in_threadpool(
        gmail.initiate_connection, str(user.id), return_url
    )
    return GmailConnect(redirect_url=redirect_url)


@router.get("/gmail/callback", include_in_schema=False)
async def gmail_callback(
    user_id: str = "", status: str = "", connected_account_id: str = ""
) -> RedirectResponse:
    """Composio's OAuth return URL — starts onboarding, then returns the user.

    This is the only server-side signal that a grant completed, so it is what
    provisions labels, backfills recent mail, and installs the Gmail trigger.
    Nothing needs to ask for that work.

    Unauthenticated by necessity: the browser arrives here on the public origin,
    without the app's session cookie. `user_id` therefore comes from the query
    string and is not trusted — onboarding runs only if that user genuinely has
    a Gmail connection, so a forged id achieves nothing beyond a redundant sync
    for an already-connected account.
    """
    landing = f"{settings.FRONTEND_BASE_URL.rstrip('/')}/onboarding/connect"

    if status.lower() not in _GRANT_OK:
        log.warning("gmail.callback_grant_failed", user_id=user_id, status=status)
        return RedirectResponse(landing, status_code=302)

    connection = (
        await run_in_threadpool(gmail.get_active_connection, user_id) if user_id else None
    )
    if not connection:
        log.warning("gmail.callback_no_connection", user_id=user_id)
        return RedirectResponse(landing, status_code=302)

    sync_last_7_days.apply_async((user_id,), countdown=_ONBOARD_DELAY_SECONDS)
    log.info(
        "gmail.onboarding_queued",
        user_id=user_id,
        connected_account_id=connected_account_id or getattr(connection, "id", None),
    )
    return RedirectResponse(landing, status_code=302)


@router.get("/calendar/status", response_model=CalendarStatus)
async def calendar_status(user: CurrentUser) -> CalendarStatus:
    """Whether the current user has an ACTIVE Composio Google Calendar connection."""
    connected = await run_in_threadpool(calendar.is_connected, str(user.id))
    return CalendarStatus(connected=connected)


@router.get("/calendar/connect", response_model=CalendarConnect)
async def calendar_connect(user: CurrentUser) -> CalendarConnect:
    """Start the Google Calendar OAuth grant; returns a URL to send the user to."""
    return_url = f"{settings.FRONTEND_BASE_URL}/onboarding/connect"
    redirect_url = await run_in_threadpool(
        calendar.initiate_connection, str(user.id), return_url
    )
    return CalendarConnect(redirect_url=redirect_url)


