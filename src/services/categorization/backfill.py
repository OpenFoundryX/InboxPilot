"""Enqueue classification for mail that does not yet carry one of our labels.

Extracted from `workers.jobs.sync_last_7_days` so onboarding and the on-demand
re-classify share one implementation instead of two that drift.
"""

from core.logging import get_logger
from services.mailman import gmail_ops
from workers.jobs.classify_new_email import classify_new_email

log = get_logger(__name__)

# Ceiling on how much mail one backfill will classify. The fetch itself may
# return far more (see gmail.FETCH_ALL_CAP); classifying all of it would mean
# thousands of LLM calls. Newest mail is the mail that matters.
BACKFILL_CLASSIFY_MAX = 200


def queue_unlabelled(
    user_id: str,
    emails: list,
    label_names: list[str],
    limit: int = BACKFILL_CLASSIFY_MAX,
) -> int:
    """Enqueue one classify task per message lacking any of `label_names`.

    Returns how many were queued. Messages already carrying one of the user's
    category labels are skipped, so re-running is cheap and never re-decides
    mail the user has already seen categorized.
    """
    known = {lid for name in label_names if (lid := gmail_ops.resolve_label_id(user_id, name))}

    queued = 0
    for email in emails:
        if queued >= limit:
            break
        if not email.id or known.intersection(email.labels or []):
            continue
        classify_new_email.delay(
            user_id,
            email.id,
            sender=email.sender,
            subject=email.subject,
            snippet=email.snippet,
        )
        queued += 1
    return queued
