"""Celery task: bring already-classified mail in line with a category's
"move out of my inbox" setting.

The per-category archive flag is applied by the classifier at the moment an
email is categorized, so on its own it only ever affects *new* mail. When the
user flips the toggle they mean their mailbox, not just future arrivals — this
task closes that gap by moving the mail already carrying the label.

No LLM involved: nothing is re-classified. It only adds or removes INBOX.
"""

from core.logging import get_logger
from integrations.composio import gmail
from services.mailman import gmail_ops
from workers.celery_app import celery_app

log = get_logger(__name__)

# Ceiling on one sweep. A mailbox with more than this in a single category is
# unusual; the rest gets picked up next time the toggle is flipped.
SYNC_MAX = 500
# Composio rejects very large batchModify payloads, so move in chunks.
CHUNK = 100


def _chunks(items: list[str], size: int):
    for start in range(0, len(items), size):
        yield items[start : start + size]


@celery_app.task(
    name="categorization.sync_category_inbox",
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=3,
)
def sync_category_inbox(user_id: str, gmail_label: str, archive: bool) -> dict:
    """Move mail carrying `gmail_label` to the side of the inbox `archive` implies."""
    # Only fetch what is on the wrong side — re-running is then nearly free.
    scope = "in:inbox" if archive else "-in:inbox"
    query = f'label:"{gmail_label}" {scope}'

    emails = gmail.fetch_by_query(user_id, query, max_results=SYNC_MAX)
    message_ids = [e.id for e in emails if e.id]

    if not message_ids:
        log.info("categorization.sync_noop", user_id=user_id, label=gmail_label)
        return {"user_id": user_id, "label": gmail_label, "moved": 0}

    for chunk in _chunks(message_ids, CHUNK):
        gmail_ops.set_inbox_state(user_id, chunk, in_inbox=not archive)

    log.info(
        "categorization.sync_applied",
        user_id=user_id,
        label=gmail_label,
        archive=archive,
        moved=len(message_ids),
    )
    return {"user_id": user_id, "label": gmail_label, "moved": len(message_ids)}
