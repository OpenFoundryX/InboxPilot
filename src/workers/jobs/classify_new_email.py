"""Celery task: label one message that just arrived, driven by the Gmail trigger.

Everything needed comes from the webhook payload, so this never fetches from
Gmail to decide — it only calls Gmail to apply the label. Safe to retry:
re-applying a label is a no-op.
"""

from core.logging import get_logger
from services.classify.apply import classify_and_label
from workers.celery_app import celery_app

log = get_logger(__name__)


@celery_app.task(
    name="classify.new_email",
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=3,
)
def classify_new_email(
    user_id: str,
    message_id: str,
    sender: str | None = None,
    subject: str | None = None,
    snippet: str | None = None,
) -> dict:
    label = classify_and_label(
        user_id,
        message_id=message_id,
        sender=sender,
        subject=subject,
        snippet=snippet,
    )
    return {"user_id": user_id, "message_id": message_id, "label": label}
