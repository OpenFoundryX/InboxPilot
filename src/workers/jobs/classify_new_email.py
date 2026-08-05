"""Celery task: label one message that just arrived, driven by the Gmail trigger.

Everything needed comes from the webhook payload, so this never fetches from
Gmail to decide — it only calls Gmail to apply the label. Safe to retry:
re-applying a label is a no-op.

This task used to chain `drafts.reply` once it had a category, so a draft was
written the moment mail landed. It no longer does: drafting is scheduled, and
`drafts.sweep` is the only producer (see `workers.jobs.drafts_sweep`). Labelling
is cheap and wants to be immediate; drafting is an LLM call per message whose
output lands in the user's mailbox, and drafts arriving one at a time as mail
trickles in is the same interruption the product batches mail delivery to
avoid. Classification therefore knows nothing about drafting any more.
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
    thread_id: str | None = None,
) -> dict:

    log.info(f"Classifying new email for user {user_id} with message_id {message_id}")
    label = classify_and_label(
        user_id,
        message_id=message_id,
        sender=sender,
        subject=subject,
        snippet=snippet,
        thread_id=thread_id,
    )
    log.info(f"Classified new email for user {user_id} with message_id {message_id} as {label}")

    return {"user_id": user_id, "message_id": message_id, "label": label}
