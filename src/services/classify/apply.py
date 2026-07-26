"""Classify one message and apply the resulting org label.

The single implementation shared by the webhook task (mail arriving now) and the
onboarding backfill (mail that arrived before the user connected). Blocking
Composio + OpenAI calls — invoke from a Celery task.
"""

from functools import lru_cache

from core.logging import get_logger
from integrations.composio import gmail
from services.classify.classifier import classify
from services.mailman import gmail_ops

log = get_logger(__name__)


@lru_cache(maxsize=256)
def _ensure_labels_once(user_id: str) -> bool:
    """Provision the org labels for a user, at most once per worker process.

    The classifier can only apply a label that exists in the user's Gmail.
    Accounts that skipped (or failed) the initial sync would otherwise fail with
    `label '<name>' not found`. Idempotent and cheap (one LIST plus creates for
    whatever is missing). A raised error is not cached, so it retries next time.
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
) -> str | None:
    """Label one message. Returns the label applied, or None if undecided."""
    _ensure_labels_once(user_id)

    label = classify(sender, subject, snippet)
    if not label:
        return None

    gmail_ops.add_label(user_id, [message_id], label)
    log.info("classify.labeled", user_id=user_id, message_id=message_id, label=label)
    return label
