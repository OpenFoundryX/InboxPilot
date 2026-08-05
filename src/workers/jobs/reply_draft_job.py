"""Celery task: draft a reply to one message that just arrived.

Chained off `classify.new_email` once the category is known — drafting is gated
on the category, so it cannot run before classification has picked one.

Everything needed comes from the webhook payload, including the message body
(`message_text`), so this never fetches from Gmail to decide. Safe to retry:
`uq_draft_replies_user_source_kind` means a retry that races itself produces one
draft, not two.
"""

from core.logging import get_logger
from services.drafts.create import draft_reply
from workers.celery_app import celery_app

log = get_logger(__name__)


@celery_app.task(
    name="drafts.reply",
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=3,
)
def reply_draft(
    user_id: str,
    message_id: str,
    sender: str | None = None,
    subject: str | None = None,
    body: str | None = None,
    thread_id: str | None = None,
    category_key: str | None = None,
    to: str | None = None,
    cc: str | None = None,
) -> dict:
    draft_id = draft_reply(
        user_id,
        message_id=message_id,
        sender=sender,
        subject=subject,
        body=body,
        to=to,
        cc=cc,
        thread_id=thread_id,
        category_key=category_key,
    )
    return {"user_id": user_id, "message_id": message_id, "gmail_draft_id": draft_id}
