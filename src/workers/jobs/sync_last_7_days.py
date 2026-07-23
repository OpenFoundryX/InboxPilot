"""Celery job: fetch a user's Gmail messages from the last 7 days."""

from core.logging import get_logger
from integrations.composio import gmail
from workers.celery_app import celery_app

log = get_logger(__name__)


@celery_app.task(name="jobs.sync_last_7_days")
def sync_last_7_days(user_id: str, max_results: int = 100) -> dict:
    """Fetch last-7-days emails for `user_id` (the Composio entity / app user id).

    Returns a small summary; the emails themselves are included so the result
    is visible via the result backend while there's no persistence layer yet.
    """
    # Best-effort: make sure InboxPilot's labels exist in the user's Gmail.
    # A failure here must never block the email fetch, so it's isolated.
    try:
        created = gmail.ensure_labels(user_id)
        if created:
            log.info("gmail.labels_provisioned", user_id=user_id, created=created)
    except Exception:
        log.exception("gmail.ensure_labels_failed", user_id=user_id)

    emails = gmail.fetch_recent_emails(user_id, days=7, max_results=max_results)
    log.info("gmail.sync_last_7_days", user_id=user_id, count=len(emails))
    return {
        "user_id": user_id,
        "count": len(emails),
        # model_dump(mode="json") so datetimes etc. are Celery/JSON-serializable
        "emails": [e.model_dump(mode="json") for e in emails],
    }
