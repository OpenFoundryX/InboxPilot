"""Classify one message and apply the resulting category label.

The single implementation shared by the webhook task (mail arriving now) and the
onboarding backfill (mail that arrived before the user connected). Blocking
Gmail + OpenAI calls — invoke from a Celery task.
"""

from functools import lru_cache

from core.logging import get_logger
from integrations.google import gmail
from services.categorization import pipeline

log = get_logger(__name__)


@lru_cache(maxsize=256)
def _ensure_labels_once(user_id: str) -> bool:
    """Provision the built-in org labels for a user, at most once per process.

    The classifier can only apply a label that exists in the user's Gmail.
    Accounts that skipped (or failed) the initial sync would otherwise fail with
    `label '<name>' not found`. Idempotent and cheap (one LIST plus creates for
    whatever is missing). A raised error is not cached, so it retries next time.

    Custom categories are not covered here — their Gmail label is created
    synchronously when the category is created, so this cache never goes stale.
    """
    gmail.ensure_labels(user_id)
    return True


def classify_and_label(
    user_id: str,
    *,
    message_id: str,
    sender: str | None,
    subject: str | None,
    snippet: str | None,
    thread_id: str | None = None,
) -> str | None:
    """Label one message. Returns the category key applied, or None.

    `thread_id` scopes the label to the whole conversation, which is what keeps
    a thread showing exactly one category — see `gmail_ops.apply_category`.
    """
    _ensure_labels_once(user_id)
    return pipeline.categorize_and_apply(
        user_id,
        message_id=message_id,
        sender=sender,
        subject=subject,
        snippet=snippet,
        thread_id=thread_id,
    )
