"""Catch-up pass: draft for mail in the chosen categories that has no draft yet.

The arrival job handles mail as it lands, so this exists for the gaps — drafting
switched on after the mail arrived, a category added later, a worker that was
down, an LLM call that failed all its retries.

One Gmail query per selected category rather than one combined query. The
combined form would be cheaper, but `EmailSummary.labels` carries Gmail *label
ids* (`Label_17`), not names, so a batched result could not be attributed back to
a category without a separate label lookup. Querying per label means the category
is known by construction, and users typically select two.
"""

import uuid

from core.database import run_async, with_worker_session
from core.logging import get_logger
from integrations.composio import gmail
from services.categorization.store import get_or_create_categories
from services.drafts.context import DraftConfig
from services.drafts.create import draft_reply

log = get_logger(__name__)

# How far back a catch-up pass looks. Long enough to cover a worker outage or a
# setting flipped on this morning, short enough that enabling drafts does not
# retroactively fill the mailbox with replies to week-old mail.
LOOKBACK_DAYS = 2
# Candidates pulled per category.
MAX_PER_CATEGORY = 25
# Drafts created per user per sweep, across all categories. Bounds LLM spend and
# keeps a backlog from arriving as one wall of drafts.
MAX_PER_SWEEP = 10


async def _labels_for_keys(db, user_id: uuid.UUID, keys: tuple[str, ...]) -> list[tuple[str, str]]:
    """Map selected category keys to (key, gmail_label), skipping disabled ones."""
    categories = await get_or_create_categories(db, user_id)
    return [(c.key, c.gmail_label) for c in categories if c.key in keys and c.is_enabled]


def sweep_user(user_id: str, config: DraftConfig, user_name: str | None = None) -> int:
    """Draft for one user's undrafted recent mail. Returns how many were created."""
    if not config.category_keys:
        return 0

    uid = uuid.UUID(user_id)
    targets: list[tuple[str, str]] = run_async(
        with_worker_session(lambda db: _labels_for_keys(db, uid, config.category_keys))
    )
    if not targets:
        return 0

    created = 0
    for category_key, gmail_label in targets:
        if created >= MAX_PER_SWEEP:
            break
        # `-label:DRAFTED_LABEL` is what makes this pass idempotent. Nothing about
        # drafts is stored, so without this exclusion every sweep would re-draft
        # the same mail — a fresh reply to the same email every 15 minutes.
        query = (
            f'label:"{gmail_label}" newer_than:{LOOKBACK_DAYS}d '
            f'-in:sent -in:draft -label:"{gmail.DRAFTED_LABEL}"'
        )
        try:
            emails = gmail.fetch_by_query(user_id, query, MAX_PER_CATEGORY, verbose=True)
        except Exception:
            log.exception("drafts.sweep_fetch_failed", user_id=user_id, label=gmail_label)
            continue

        for email in emails:
            if created >= MAX_PER_SWEEP:
                break
            if not email.id:
                continue
            try:
                draft_id = draft_reply(
                    user_id,
                    message_id=email.id,
                    sender=email.sender,
                    subject=email.subject,
                    body=email.body or email.snippet,
                    thread_id=email.thread_id,
                    category_key=category_key,
                    user_name=user_name,
                    config=config,
                )
            except Exception:
                # One bad message must not end the sweep for the rest.
                log.exception("drafts.sweep_draft_failed", user_id=user_id, message_id=email.id)
                continue
            if draft_id:
                created += 1

    if created:
        log.info("drafts.sweep_created", user_id=user_id, count=created)
    return created
