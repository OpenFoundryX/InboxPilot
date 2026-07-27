"""Celery job: onboard a connected Gmail account.

The only non-webhook mail path left. It provisions labels, classifies the mail
that arrived before the user connected, and installs the trigger that makes
everything after this webhook-driven. Re-running it is also the manual catch-up
lever if events were missed — already-labelled mail is skipped, so a second run
is cheap.
"""

from core.logging import get_logger
from integrations.composio import gmail
from integrations.composio.triggers import ensure_gmail_new_message_trigger
from services.categorization import backfill
from services.categorization.pipeline import get_config
from workers.celery_app import celery_app

log = get_logger(__name__)


@celery_app.task(name="jobs.sync_last_7_days")
def sync_last_7_days(user_id: str, days: int = 30, max_results: int | None = None) -> dict:
    """Fetch recent emails for `user_id` (the Composio entity / app user id).

    Defaults to the full last-30-days window (paginated). Pass `max_results` to
    cap the fetch. Returns a summary; email bodies are metadata-only.
    """
    try:
        sync = gmail.ensure_labels(user_id)
        if sync.created:
            log.info("gmail.labels_provisioned", user_id=user_id, created=sync.created)
    except Exception:
        log.exception("gmail.ensure_labels_failed", user_id=user_id)

    emails = gmail.fetch_recent_emails(user_id, days=days, max_results=max_results)
    config = get_config(user_id)
    queued = backfill.queue_unlabelled(
        user_id, emails, [c.gmail_label for c in config.categories]
    )
    trigger_id, trigger_error = _install_trigger(user_id)

    log.info(
        "gmail.sync_last_7_days",
        user_id=user_id,
        days=days,
        count=len(emails),
        classified=queued,
    )
    return {
        "user_id": user_id,
        "count": len(emails),
        "classified": queued,
        "trigger": trigger_id,
        "trigger_error": trigger_error,
        "emails": [e.model_dump() if hasattr(e, "model_dump") else e for e in emails],
    }


def _install_trigger(user_id: str) -> tuple[str | None, str | None]:
    """Install the Gmail trigger. A failure here means the user gets no mail."""
    try:
        return ensure_gmail_new_message_trigger(user_id), None
    except Exception as exc:
        log.exception("gmail.trigger_install_failed", user_id=user_id)
        return None, str(exc)
