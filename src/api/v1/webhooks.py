"""Inbound webhooks (Composio triggers, meeting-bot status callbacks).

The arrival path for all mail. Composio polls Gmail and posts one event per new
message; we verify it, drop what we've already handled or sent ourselves, and
hand the work to a Celery task. Nothing here may block: routing decides on the
payload's `label_ids` alone, and the deeper loop guard (which needs a Gmail
label lookup) lives inside the command task.

The meeting-bot provider posts here too, reporting a bot's progress through a
call. Same rule: verify, record the status, enqueue the real work.
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select

from api.deps import DbSession
from core.idempotency import claim_event, is_ours
from core.logging import get_logger
from integrations.composio import triggers as composio_triggers
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
from workers.jobs.classify_new_email import classify_new_email
from workers.jobs.handle_command_email import handle_command_email
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


SENT_LABEL = "SENT"
SNIPPET_CHARS = 200


@router.post("/composio", status_code=status.HTTP_200_OK)
async def composio_webhook(request: Request) -> dict[str, str]:
    """Receive Composio trigger events; enqueue work for new Gmail messages."""
    try:
        result = composio_triggers.parse_webhook(await request.body(), request.headers)
    except Exception as exc:
        log.warning("composio.webhook_reject", error=str(exc))
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid webhook") from exc

    event = result.get("payload")
    if event.get("trigger_slug") != composio_triggers.GMAIL_NEW_MESSAGE:
        return {"status": "ignored"}

    user_id = event.get("user_id")
    data = event.get("payload")
    message_id = data.get("id")

    if not user_id or not message_id:
        return {"status": "ignored"}

    label_ids = list(data.get("label_ids"))
    is_command = SENT_LABEL in label_ids

    try:
        if await is_ours(message_id):
            log.info("composio.webhook_own_message", user_id=user_id, message_id=message_id)
            return {"status": "skipped_own"}
        if not await claim_event(user_id, message_id):
            return {"status": "duplicate"}

    except Exception:
        log.exception("composio.webhook_guards_unavailable", user_id=user_id, message_id=message_id)
        if is_command:
            return {"status": "guards_unavailable"}

    if is_command:
        handle_command_email.delay(
            str(user_id),
            str(message_id),
            subject=data.get("subject"),
            body=data.get("message_text"),
            thread_id=data.get("thread_id"),
            label_ids=label_ids,
        )
        log.info("composio.webhook_command", user_id=user_id, message_id=message_id)
        return {"status": "command"}

    classify_new_email.delay(
        str(user_id),
        str(message_id),
        sender=data.get("sender"),
        subject=data.get("subject"),
        snippet=_snippet(data),
    )

    log.info("composio.webhook_classify", user_id=user_id, message_id=message_id)

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
    # The bot id is how a later callback finds this row when metadata is absent.
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


def _snippet(data: dict) -> str | None:
    """Short preview for the classifier — never the whole body."""

    preview = data.get("preview")
    text = preview.get("body")
    text = text or data.get("message_text")
    return text.strip()[:SNIPPET_CHARS]
