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
from services.classify.classifier import LABEL_NAMES
from services.mailman import gmail_ops
from workers.celery_app import celery_app
from workers.jobs.classify_new_email import classify_new_email

log = get_logger(__name__)

# Ceiling on backfill classification. The fetch itself may return far more (see
# gmail.FETCH_ALL_CAP); classifying all of it would mean thousands of LLM calls
# on connect. Newest mail is the mail that matters.
BACKFILL_CLASSIFY_MAX = 200


@celery_app.task(name="jobs.sync_last_7_days")
def sync_last_7_days(user_id: str, days: int = 30, max_results: int | None = None) -> dict:
    """Fetch recent emails for `user_id` (the Composio entity / app user id).

    Defaults to the full last-30-days window (paginated). Pass `max_results` to
    cap the fetch. Returns a summary; email bodies are metadata-only.
    """
    try:
        created = gmail.ensure_labels(user_id)
        if created:
            log.info("gmail.labels_provisioned", user_id=user_id, created=created)
    except Exception:
        log.exception("gmail.ensure_labels_failed", user_id=user_id)

    emails = gmail.fetch_recent_emails(user_id, days=days, max_results=max_results)
    queued = _queue_backfill_classification(user_id, emails)
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


def _queue_backfill_classification(user_id: str, emails: list) -> int:
    """Enqueue one classify task per unlabelled message, newest first."""
    known = {lid for name in LABEL_NAMES if (lid := gmail_ops.resolve_label_id(user_id, name))}

    queued = 0
    for e in emails:
        if queued >= BACKFILL_CLASSIFY_MAX:
            break
        if not e.id or known.intersection(e.labels or []):
            continue
        classify_new_email.delay(
            user_id, e.id, sender=e.sender, subject=e.subject, snippet=e.snippet
        )
        queued += 1
    return queued


def _install_trigger(user_id: str) -> tuple[str | None, str | None]:
    """Install the Gmail trigger. A failure here means the user gets no mail."""
    try:
        return ensure_gmail_new_message_trigger(user_id), None
    except Exception as exc:
        log.exception("gmail.trigger_install_failed", user_id=user_id)
        return None, str(exc)
