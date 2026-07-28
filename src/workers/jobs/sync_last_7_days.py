"""Celery job: onboard a connected Gmail account.

The only non-webhook mail path left. It provisions labels, classifies the mail
that arrived before the user connected, and installs the trigger that makes
everything after this webhook-driven. Re-running it is also the manual catch-up
lever if events were missed — already-labelled mail is skipped, so a second run
is cheap.
"""

import uuid
from datetime import datetime, timezone

from core.database import run_async, with_worker_session
from core.logging import get_logger
from integrations.composio import gmail
from integrations.composio.triggers import ensure_gmail_new_message_trigger
from models.categorization import BUILTIN_CATEGORIES
from models.users import User
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

    # get_config is a DB round trip and, unlike ensure_labels above, has no
    # autoretry. A transient DB error here must not abort the task before
    # _install_trigger() runs below — missing the trigger means the user gets
    # no mail at all, which is worse than falling back to the built-in labels.
    try:
        label_names = [c.gmail_label for c in get_config(user_id).categories]
    except Exception:
        log.exception("categorization.config_unavailable", user_id=user_id)
        label_names = [c.gmail_label for c in BUILTIN_CATEGORIES]

    queued = backfill.queue_unlabelled(user_id, emails, label_names)
    trigger_id, trigger_error = _install_trigger(user_id)
    _stamp_initial_sync(user_id)

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


def _stamp_initial_sync(user_id: str) -> None:
    """Mark onboarding complete. Guarded like the other DB work in this task:
    a failed stamp must not lose the trigger install or the classified mail."""

    async def _write(db) -> None:
        user = await db.get(User, uuid.UUID(user_id))
        if user is not None and user.initial_sync_at is None:
            user.initial_sync_at = datetime.now(timezone.utc)

    try:
        run_async(with_worker_session(_write))
    except Exception:
        log.exception("gmail.initial_sync_stamp_failed", user_id=user_id)
