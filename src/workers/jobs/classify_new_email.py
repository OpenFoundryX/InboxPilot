"""Celery task: label one message that just arrived, driven by the Gmail trigger.

Everything needed comes from the webhook payload, so this never fetches from
Gmail to decide — it only calls Gmail to apply the label. Safe to retry:
re-applying a label is a no-op.

Auto-drafting is chained on from here rather than from the webhook, because it is
gated on the category and so cannot start until this task has picked one.
"""

from core.logging import get_logger
from services.classify.apply import classify_and_label
from workers.celery_app import celery_app
from workers.jobs.reply_draft_job import reply_draft

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
    body: str | None = None,
) -> dict:
    label = classify_and_label(
        user_id,
        message_id=message_id,
        sender=sender,
        subject=subject,
        snippet=snippet,
        thread_id=thread_id,
    )

    # A separate task, not an inline call: drafting spends an LLM call and a
    # Gmail write, and this task is retried on any exception. Inlining it would
    # make a drafting failure re-run classification, and each retry would then
    # re-attempt the draft. Enqueueing keeps the two failure domains apart.
    #
    # Queued whenever a category was applied; `draft_reply` owns the decision
    # about whether this user wants a draft for that category. Deciding here
    # would need the draft config loaded on every classified message, drafting
    # user or not.
    if label:
        reply_draft.delay(
            user_id,
            message_id,
            sender=sender,
            subject=subject,
            # The full body from the webhook payload. The classifier deliberately
            # sees only a truncated preview, but a draft written from a 200-char
            # snippet would be replying to a message it has not read.
            body=body or snippet,
            thread_id=thread_id,
            category_key=label,
        )

    return {"user_id": user_id, "message_id": message_id, "label": label}
