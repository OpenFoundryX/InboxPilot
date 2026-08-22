"""Celery job: onboard a connected Gmail account.

The only non-polling mail path left. It provisions labels, classifies the mail
that arrived before the user connected, and seeds the history cursor the poller
resumes from. Re-running it is also the manual catch-up lever if messages were
missed — already-labelled mail is skipped, so a second run is cheap.
"""

import uuid
from datetime import datetime, timezone

from core.database import run_async, with_worker_session
from core.logging import get_logger
from integrations.google import gmail
from integrations.google.credentials import set_history_id
from workers.jobs.gmail_poll import install_watch
from models.categorization import BUILTIN_CATEGORIES
from models.users import User
from services.billing.gate import mail_gate_open
from services.categorization import backfill
from services.categorization.pipeline import get_config
from workers.celery_app import celery_app

log = get_logger(__name__)


@celery_app.task(name="jobs.sync_last_7_days")
def sync_last_7_days(user_id: str, days: int = 30, max_results: int | None = None) -> dict:
    """Fetch recent emails for `user_id` (the app user id).

    Defaults to the full last-30-days window (paginated). Pass `max_results` to
    cap the fetch. Returns a summary; email bodies are metadata-only.
    """
    # Re-checked here rather than trusted from the caller: this task is enqueued
    # the moment onboarding and checkout both complete, and a trial can be
    # cancelled between the enqueue and the run.
    if not mail_gate_open(user_id):
        log.info("gmail.sync_skipped_gated", user_id=user_id)
        return {"skipped": "gated"}

    try:
        sync = gmail.ensure_labels(user_id)
        if sync.created:
            log.info("gmail.labels_provisioned", user_id=user_id, created=sync.created)
    except Exception:
        log.exception("gmail.ensure_labels_failed", user_id=user_id)

    emails = gmail.fetch_recent_emails(user_id, days=days, max_results=max_results)

    try:
        label_names = [c.gmail_label for c in get_config(user_id).categories]
    except Exception:
        log.exception("categorization.config_unavailable", user_id=user_id)
        label_names = [c.gmail_label for c in BUILTIN_CATEGORIES]

    queued = backfill.queue_unlabelled(user_id, emails, label_names)
    history_id, history_error = _seed_history_cursor(user_id)
    # Order matters: the cursor has to exist before push starts firing, or the
    # first notification arrives for a mailbox the poller will skip.
    watching = install_watch(user_id)
    _stamp_initial_sync(user_id)

    log.info("gmail.sync_last_7_days", user_id=user_id, days=days, count=len(emails), classified=queued)

    return {
        "user_id": user_id,
        "count": len(emails),
        "classified": queued,
        "history_id": history_id,
        "history_error": history_error,
        "watching": watching,
        "emails": [e.model_dump() if hasattr(e, "model_dump") else e for e in emails],
    }


def _seed_history_cursor(user_id: str) -> tuple[str | None, str | None]:
    """Record where the poller should start. Without it, no mail is processed.

    Written only if unset: the connect callback seeds it too, and a re-run of
    this task must not rewind a cursor that has since moved forward — that would
    replay every message since, which the event claim would absorb but at real
    Gmail quota cost.
    """
    try:
        history_id = str(gmail.get_profile(user_id).get("historyId") or "")
        if not history_id:
            return None, "no historyId on profile"
        set_history_id(user_id, history_id, only_if_unset=True)
        return history_id, None
    except Exception as exc:
        log.exception("gmail.history_seed_failed", user_id=user_id)
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
