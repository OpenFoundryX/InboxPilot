"""Integration routes (API v1) — the Google grant behind Gmail and Calendar.

One OAuth grant covers both products. `/google/connect` starts it and
`/google/callback` stores it; `/gmail/status` and `/calendar/status` remain as
separate questions with separate answers, because incremental auth means a user
can hold one scope set and not the other.
"""

import secrets
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse

from api.deps import DbSession
from core.config import settings
from core.logging import get_logger
from core.redis import redis_client
from integrations.google import credentials as google_credentials
from integrations.google import oauth as google_oauth
from models.google import CALENDAR_REQUIRED_SCOPES, GMAIL_REQUIRED_SCOPES
from models.users import User
from schemas.integrations import (
    CalendarConnect,
    CalendarStatus,
    GmailConnect,
    GmailStatus,
    GoogleConnect,
    GoogleStatus,
)
from services.auth.dependencies import get_current_user

log = get_logger(__name__)

# CSRF state for the Google grant. Redis rather than a cookie because the
# callback lands on PUBLIC_BASE_URL, which the app's session cookie is not
# scoped to.
_STATE_PREFIX = "goauth:"
_STATE_TTL_SECONDS = 600

router = APIRouter(prefix="/integrations", tags=["integrations"])

CurrentUser = Annotated[User, Depends(get_current_user)]


@router.get("/gmail/status", response_model=GmailStatus)
async def gmail_status(user: CurrentUser) -> GmailStatus:
    """Whether Gmail is connected, and whether new mail actually reaches us.

    `connected` without `listening` is the silent-failure state: the grant is
    fine, but no history cursor was ever seeded, so the poller skips this
    mailbox and no mail is processed at all. Reconnecting re-runs onboarding,
    which seeds it.
    """
    state = await run_in_threadpool(google_credentials.get_connection, str(user.id))
    if state is None or state.revoked or not GMAIL_REQUIRED_SCOPES <= state.scopes:
        return GmailStatus(connected=False, listening=False)
    return GmailStatus(connected=True, listening=state.history_id is not None)


@router.get("/gmail/connect", response_model=GmailConnect)
async def gmail_connect(user: CurrentUser) -> GmailConnect:
    """Start the grant. Gmail and Calendar are one consent, so this is /google/connect."""
    return GmailConnect(redirect_url=await _connect_url(user))


@router.get("/google/connect", response_model=GoogleConnect)
async def google_connect(user: CurrentUser) -> GoogleConnect:
    """Start the one grant that covers Gmail and Calendar.

    Replaces connecting the two products separately: the consent screen asks for
    mail and calendar access together, so a user clicks through once.
    """
    return GoogleConnect(redirect_url=await _connect_url(user))


@router.get("/google/callback", include_in_schema=False)
async def google_callback(
    db: DbSession, code: str = "", state: str = "", error: str = ""
) -> RedirectResponse:
    """Google's OAuth return URL — stores the grant, then starts onboarding.

    Unauthenticated by necessity: the browser arrives on the public origin
    without the app's session cookie. The user is therefore *not* taken from the
    query string — `state` is a single-use random token this server issued and
    stored against a user id, so a caller cannot nominate whose account a grant
    attaches to.

    The grant's own identity is checked too: Google's picker will happily let
    someone signed in as one account consent as another, and without this the
    app would attach a stranger's mailbox to the session and every later call
    would quietly operate on the wrong inbox.
    """
    landing = f"{settings.FRONTEND_BASE_URL.rstrip('/')}/onboarding/connect"

    if error or not code or not state:
        log.warning("google.callback_declined", error=error or "missing_code")
        return RedirectResponse(f"{landing}?connected=0", status_code=302)

    # Single-use: pop rather than get, so a replayed callback cannot re-run the
    # exchange.
    user_id = await redis_client.getdel(f"{_STATE_PREFIX}{state}")
    if not user_id:
        log.warning("google.callback_bad_state")
        return RedirectResponse(f"{landing}?connected=0", status_code=302)

    try:
        grant = await run_in_threadpool(google_oauth.exchange_code, code)
    except Exception:
        log.exception("google.callback_exchange_failed", user_id=user_id)
        return RedirectResponse(f"{landing}?connected=0", status_code=302)

    if not await _grant_matches_user(db, user_id, grant.google_sub):
        log.warning("google.callback_account_mismatch", user_id=user_id)
        return RedirectResponse(f"{landing}?connected=0&reason=account_mismatch", status_code=302)

    try:
        await run_in_threadpool(google_credentials.store_grant, user_id, grant)
    except Exception:
        # The user did everything right and Google granted everything asked
        # for; the failure is ours. Saying "try again" would send them round
        # the same loop forever, so this reports a server fault instead.
        log.exception("google.callback_store_failed", user_id=user_id)
        return RedirectResponse(
            f"{landing}?connected=0&reason=server_error", status_code=302
        )

    # Deliberately does NOT start the mailbox sync. Connecting is consent to
    # read the mailbox, not permission to start working in it, and this
    # callback fires before the plan step — so firing here labelled the mail of
    # users who never reached checkout. `services.billing.gate` owns the start
    # now, triggered by whichever of onboarding-complete / trial-start lands
    # second.
    log.info("google.connected", user_id=user_id, email=grant.email, scopes=sorted(grant.scopes))
    return RedirectResponse(f"{landing}?connected=1", status_code=302)


@router.get("/google/status", response_model=GoogleStatus)
async def google_status(user: CurrentUser) -> GoogleStatus:
    """What the current user's Google grant covers, if anything."""
    state = await run_in_threadpool(google_credentials.get_connection, str(user.id))
    if state is None:
        return GoogleStatus(connected=False)

    if state.revoked:
        return GoogleStatus(connected=False, needs_reconnect=True, email=state.email)

    return GoogleStatus(
        connected=True,
        gmail=GMAIL_REQUIRED_SCOPES <= state.scopes,
        calendar=CALENDAR_REQUIRED_SCOPES <= state.scopes,
        listening=state.history_id is not None,
        email=state.email,
    )


@router.post("/google/disconnect", status_code=204)
async def google_disconnect(user: CurrentUser) -> None:
    """Forget the grant and ask Google to invalidate it."""
    state = await run_in_threadpool(google_credentials.get_connection, str(user.id))
    if state is None:
        return
    if state.refresh_token:
        await run_in_threadpool(google_oauth.revoke, state.refresh_token)
    await run_in_threadpool(google_credentials.disconnect, str(user.id))
    log.info("google.disconnected", user_id=str(user.id))


async def _grant_matches_user(db: DbSession, user_id: str, google_sub: str) -> bool:
    """Whether the consenting Google account is the one the user signed in as.

    A user with no `google_sub` cannot be checked — they signed up some other
    way — so the grant is accepted and its identity becomes the record.
    """
    try:
        found = await db.get(User, uuid.UUID(user_id))
    except ValueError:
        return False
    if found is None:
        return False
    return found.google_sub is None or found.google_sub == google_sub


@router.get("/calendar/status", response_model=CalendarStatus)
async def calendar_status(user: CurrentUser) -> CalendarStatus:
    """Whether the user's grant covers Google Calendar."""
    state = await run_in_threadpool(google_credentials.get_connection, str(user.id))
    connected = bool(
        state and not state.revoked and CALENDAR_REQUIRED_SCOPES <= state.scopes
    )
    return CalendarStatus(connected=connected)


@router.get("/calendar/connect", response_model=CalendarConnect)
async def calendar_connect(user: CurrentUser) -> CalendarConnect:
    """Start the grant. Calendar and Gmail are one consent, so this is /google/connect."""
    return CalendarConnect(redirect_url=await _connect_url(user))


async def _connect_url(user: User) -> str:
    """Issue a single-use state token and build the consent URL.

    Shared by all three connect routes: there is exactly one grant, so pointing
    the older per-product routes at it keeps them working without a second
    consent screen.
    """
    state = secrets.token_urlsafe(32)
    await redis_client.set(f"{_STATE_PREFIX}{state}", str(user.id), ex=_STATE_TTL_SECONDS)
    return google_oauth.build_connect_url(state, login_hint=user.email)


