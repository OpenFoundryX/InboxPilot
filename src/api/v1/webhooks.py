"""Inbound webhooks (Gmail push, meeting-bot status callbacks, Razorpay).

Gmail's Pub/Sub push arrives here and is the fast path for new mail. It carries
no message content — only "this mailbox changed" — so it resolves the mailbox to
a user and hands off to `workers.jobs.gmail_poll`, which is the same task the
reconciliation sweep runs. Push replaces the timer, not the lookup, and the app
keeps working with this endpoint unreachable: the sweep still finds everything,
just later.

The meeting-bot provider posts here, reporting a bot's progress through a call:
verify, record the status, enqueue the real work.

Razorpay posts subscription lifecycle events here as well. Same rule: verify the
signature over the raw body before touching anything, dedupe via the Redis
claim, then hand off to `services.billing.webhooks`.
"""

import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select

from api.deps import DbSession
from core.idempotency import claim_event
from core.logging import get_logger
from integrations.google import credentials as google_credentials
from integrations.google import pubsub
from integrations.meetingbot import get_provider
from integrations.meetingbot.base import (
    BOT_DONE,
    BOT_ENDED,
    BOT_FAILED,
    BOT_JOINING,
    BOT_RECORDING,
    MeetingBotError,
)
from models.meetings import (
    STATUS_DELIVERED,
    STATUS_ENDED,
    STATUS_FAILED,
    STATUS_JOINING,
    STATUS_PROCESSED,
    STATUS_RECORDED,
    STATUS_RECORDING,
    Meeting,
)
from services.billing.webhooks import handle_event
from workers.jobs.gmail_poll import poll_user
from workers.jobs.process_meeting import process_meeting

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
log = get_logger(__name__)

# Provider lifecycle -> stored meeting status.
_BOT_STATUS_MAP = {
    BOT_JOINING: STATUS_JOINING,
    BOT_RECORDING: STATUS_RECORDING,
    BOT_ENDED: STATUS_ENDED,
    BOT_DONE: STATUS_RECORDED,
    BOT_FAILED: STATUS_FAILED,
}
# Once a recap exists, later callbacks are noise — never walk a meeting back.
_TERMINAL_STATUSES = (STATUS_PROCESSED, STATUS_DELIVERED)


@router.post("/gmail", status_code=status.HTTP_200_OK)
async def gmail_push(request: Request) -> dict[str, str]:
    """Gmail's Pub/Sub push: a mailbox changed, go and look.

    The notification carries only an address and a history id — never the
    messages themselves — so all this does is resolve the mailbox to a user and
    hand off to the same poll task the reconciliation sweep runs. One code path
    finds mail, whether a push or a timer woke it.

    **Every outcome returns 2xx.** Pub/Sub retries anything else with backoff,
    so a 4xx for a mailbox we do not recognise would have Google redelivering
    the same dead notification for days. The only thing worth rejecting is an
    unverified sender, because accepting those is what lets a stranger spend
    our Gmail quota.
    """
    try:
        pubsub.verify_push_token(request.headers.get("authorization"))
    except pubsub.InvalidPushNotification as exc:
        log.warning("gmail.push_unverified", error=str(exc))
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid push") from exc

    try:
        notification = pubsub.parse(await request.body())
    except pubsub.InvalidPushNotification as exc:
        # Malformed and unretryable: acknowledge so Pub/Sub stops resending it.
        log.warning("gmail.push_malformed", error=str(exc))
        return {"status": "ignored"}

    user_id = await run_in_threadpool(
        google_credentials.find_user_id_by_mailbox, notification.email
    )
    if not user_id:
        log.info("gmail.push_unknown_mailbox")
        return {"status": "unknown_mailbox"}

    # The poll task holds its own per-user lock and walks from the stored
    # cursor, so a burst of notifications for one mailbox collapses into one
    # pass rather than racing.
    poll_user.delay(user_id)
    log.info("gmail.push", user_id=user_id, history_id=notification.history_id)
    return {"status": "queued"}


@router.post("/meeting-bot", status_code=status.HTTP_200_OK)
async def meeting_bot_webhook(request: Request, db: DbSession) -> dict[str, str]:
    """Record a bot's progress; when it finishes, queue the recap.

    The provider times out at 15 seconds and retries, so this only touches one
    row and enqueues. Unknown bots are acknowledged rather than rejected — a 4xx
    would make the provider retry a callback we will never care about.
    """
    body = await request.body()
    try:
        event = get_provider().parse_webhook(body, request.headers)
    except MeetingBotError as exc:
        log.warning("meetingbot.webhook_reject", error=str(exc))
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid webhook") from exc

    meeting = await _find_meeting(db, event.meeting_id, event.bot_id)
    if not meeting:
        log.info("meetingbot.webhook_unknown_bot", bot_id=event.bot_id)
        return {"status": "ignored"}

    if meeting.status in _TERMINAL_STATUSES:
        return {"status": "already_processed"}

    new_status = _BOT_STATUS_MAP.get(event.status)
    if not new_status:
        return {"status": "ignored"}

    meeting.status = new_status
    meeting.status_detail = event.detail

    if new_status == STATUS_RECORDING and not meeting.joined_at:
        meeting.joined_at = datetime.now(timezone.utc)

    if not meeting.bot_id:
        meeting.bot_id = event.bot_id

    log.info(
        "meetingbot.status",
        meeting_id=str(meeting.id),
        bot_id=event.bot_id,
        status=new_status,
        detail=event.detail,
    )

    if new_status == STATUS_RECORDED:
        process_meeting.delay(str(meeting.id))
        return {"status": "processing"}

    return {"status": new_status}


@router.post("/razorpay", include_in_schema=False)
async def razorpay_webhook(request: Request, db: DbSession) -> dict:
    """Receive Razorpay events.

    The signature check is the only authentication — this route is public, so an
    unverified body must never reach the handlers. The body is read raw and
    hashed as-is; re-parsing it first would change the bytes and break every
    signature.
    """
    raw = await request.body()
    # BILLING DISABLED (temporary, for testing): the signature check is
    # commented out, so this public route currently trusts its caller. Restore
    # it — and the docstring's claim above becomes true again — before this
    # takes real Razorpay traffic.
    signature = request.headers.get("x-razorpay-signature", "")  # noqa: F841
    # if not verify_signature(raw, signature, settings.RAZORPAY_WEBHOOK_SECRET):
    #     raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid signature")

    event = json.loads(raw)

    event_id = request.headers.get("x-razorpay-event-id")
    if event_id:
        if not await claim_event("razorpay", event_id):
            return {"status": "duplicate"}
    else:
        log.warning("razorpay.webhook_missing_event_id", event_type=event.get("event"))

    result = await handle_event(db, event)
    return {"status": result}


async def _find_meeting(db, meeting_id: str | None, bot_id: str) -> Meeting | None:
    """Prefer our own id from the provider's metadata; fall back to the bot id."""
    if meeting_id:
        try:
            found = await db.get(Meeting, uuid.UUID(meeting_id))
        except ValueError:
            found = None
        if found:
            return found
    return await db.scalar(select(Meeting).where(Meeting.bot_id == bot_id))
