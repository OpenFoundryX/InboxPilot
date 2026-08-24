"""Record dashboard-countable activity. Idempotent, and never fatal to callers.

Both entry points are synchronous because every caller is Celery code. They
bridge to the async DB layer with `run_async(with_worker_session(...))` — the
same pattern `services.categorization.pipeline.get_config` uses, which exists
because a pooled connection from an earlier event loop makes asyncpg raise
"attached to a different loop".
"""

import uuid

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import run_async, with_worker_session
from core.logging import get_logger
from models.activity import KIND_DRAFT_CREATED, KIND_EMAIL_CATEGORIZED, ActivityEvent

log = get_logger(__name__)


def record_email_categorized(user_id: str, message_id: str, category_key: str) -> None:
    """Count one message as categorized. Call only after the label has landed."""
    _record(user_id, KIND_EMAIL_CATEGORIZED, message_id, category_key)


def record_draft_created(user_id: str, source_message_id: str) -> None:
    """Count one drafted reply, keyed by the email being replied to."""
    _record(user_id, KIND_DRAFT_CREATED, source_message_id, None)


def _record(user_id: str, kind: str, ref_id: str, category_key: str | None) -> None:
    """Insert one event, ignoring duplicates.

    Swallows every error by design. A statistic must never fail the work it
    describes: raising here would retry a Celery task that already labelled the
    mail successfully, re-doing Gmail work to fix a counter.
    """
    try:
        run_async(with_worker_session(lambda db: _insert(db, user_id, kind, ref_id, category_key)))
    except Exception:
        log.exception("activity.record_failed", user_id=user_id, kind=kind, ref_id=ref_id)


async def _insert(
    db: AsyncSession, user_id: str, kind: str, ref_id: str, category_key: str | None
) -> None:
    stmt = (
        insert(ActivityEvent)
        .values(
            id=uuid.uuid4(),
            user_id=uuid.UUID(user_id),
            kind=kind,
            ref_id=ref_id,
            category_key=category_key,
        )
        .on_conflict_do_nothing(constraint="uq_activity_events_user_kind_ref")
    )
    await db.execute(stmt)
