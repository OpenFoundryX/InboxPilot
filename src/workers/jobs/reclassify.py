"""Celery task: re-run categorization over a window of recent mail.

Triggered from the Categorization page after a user edits their taxonomy.
Already-categorized mail is skipped, so this is cheap to re-run and never
rewrites a decision the user has already seen.
"""

from core.logging import get_logger
from integrations.google import gmail
from services.categorization import backfill
from services.categorization.pipeline import get_config
from workers.celery_app import celery_app

log = get_logger(__name__)


@celery_app.task(name="categorization.reclassify")
def reclassify(user_id: str, days: int = 7, max_results: int | None = None) -> dict:
    config = get_config(user_id)
    if not config.is_enabled:
        log.info("reclassify.disabled", user_id=user_id)
        return {"user_id": user_id, "queued": 0, "skipped_reason": "disabled"}

    emails = gmail.fetch_recent_emails(user_id, days=days, max_results=max_results)
    queued = backfill.queue_unlabelled(
        user_id, emails, [c.gmail_label for c in config.categories]
    )

    log.info("reclassify.queued", user_id=user_id, days=days, fetched=len(emails), queued=queued)
    return {"user_id": user_id, "fetched": len(emails), "queued": queued}
