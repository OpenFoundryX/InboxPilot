"""Inbound webhooks (Composio triggers).

The arrival path for all mail. Composio polls Gmail and posts one event per new
message; we verify it, drop what we've already handled or sent ourselves, and
hand the work to a Celery task. Nothing here may block: routing decides on the
payload's `label_ids` alone, and the deeper loop guard (which needs a Gmail
label lookup) lives inside the command task.
"""

from fastapi import APIRouter, HTTPException, Request, status

from core.idempotency import claim_event, is_ours
from core.logging import get_logger
from integrations.composio import triggers as composio_triggers
from workers.jobs.classify_new_email import classify_new_email
from workers.jobs.handle_command_email import handle_command_email

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
log = get_logger(__name__)


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

    event = result.get("payload") or {}
    if event.get("trigger_slug") != composio_triggers.GMAIL_NEW_MESSAGE:
        return {"status": "ignored"}

    user_id = event.get("user_id")
    data = event.get("payload") or {}
    message_id = data.get("id")
    if not user_id or not message_id:
        return {"status": "ignored"}

    label_ids = list(data.get("label_ids") or [])
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


def _snippet(data: dict) -> str | None:
    """Short preview for the classifier — never the whole body."""
    preview = data.get("preview")
    text = preview.get("body") if isinstance(preview, dict) else None
    text = text or data.get("message_text")
    return text.strip()[:SNIPPET_CHARS] if isinstance(text, str) else None
