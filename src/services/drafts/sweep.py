"""Catch-up pass: draft for mail in the chosen categories that has no draft yet.

The arrival job handles mail as it lands, so this exists for the gaps — drafting
switched on after the mail arrived, a category added later, a worker that was
down, an LLM call that failed all its retries.

A pass clears the whole window it can see rather than trickling a fixed batch
per tick. The previous caps (25 candidates per category, 10 drafts per pass)
meant a real backlog drained over successive 15-minute ticks, so mail from two
days ago could still be waiting for its draft hours after drafting was switched
on — which defeats the point of a catch-up pass. What bounds a pass now is the
user's remaining monthly quota, with `SWEEP_SAFETY_CEILING` behind it for plans
that have no quota to bind instead.

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
# retroactively fill the mailbox with replies to week-old mail. This window is
# the real bound on how much work a pass can find.
LOOKBACK_DAYS = 2

# Candidates pulled per category. `None` makes `gmail.fetch_by_query` page until
# the query is exhausted (bounded by its own `FETCH_ALL_CAP`) instead of taking
# the newest N. With a fixed 25 a category holding more than that only ever gave
# up its newest page per tick, so a real backlog drained a page at a time over
# successive sweeps rather than in the pass that found it.
MAX_PER_CATEGORY = None

# A circuit breaker, not a throttle. A pass now drafts everything undrafted in
# the window, bounded by the user's remaining monthly quota — see
# `effective_limit`. This ceiling only binds when the plan has no quota to bind
# instead, and exists so a pathological backlog cannot start an unbounded run.
#
# It is coupled to `workers.jobs.drafts_sweep.DRAFTS_LOCK_TTL`, which is derived
# from it: raising this without that lets a pass outlive its lock, and two
# passes then spend the same stale quota snapshot. A test asserts the coupling.
SWEEP_SAFETY_CEILING = 200


async def _labels_for_keys(db, user_id: uuid.UUID, keys: tuple[str, ...]) -> list[tuple[str, str]]:
    """Map selected category keys to (key, gmail_label), skipping disabled ones."""
    categories = await get_or_create_categories(db, user_id)
    return [(c.key, c.gmail_label) for c in categories if c.key in keys and c.is_enabled]


def effective_limit(budget: int | None, max_per_sweep: int = SWEEP_SAFETY_CEILING) -> int:
    """How many drafts this pass may create.

    `budget` is what the user's plan has left this month, and it is the bound
    that normally applies — without it the sweep would happily run a whole
    backlog for someone with one draft of quota remaining.

    `max_per_sweep` defaults to this module's safety ceiling, which only takes
    effect on a plan with unlimited drafts. `follow_up.sweep_user` reuses this
    function rather than duplicating the min/max logic, but a nudge pass is not
    a backlog drain and must not inherit that ceiling, so it passes its own much
    smaller constant explicitly.
    """
    if budget is None:
        return max_per_sweep
    return min(max_per_sweep, max(0, budget))


def sweep_user(
    user_id: str,
    config: DraftConfig,
    user_name: str | None = None,
    *,
    budget: int | None = None,
) -> int:
    """Draft for one user's undrafted recent mail. Returns how many were created."""
    if not config.category_keys:
        return 0

    limit = effective_limit(budget)
    if limit <= 0:
        return 0

    uid = uuid.UUID(user_id)
    targets: list[tuple[str, str]] = run_async(
        with_worker_session(lambda db: _labels_for_keys(db, uid, config.category_keys))
    )
    if not targets:
        return 0

    created = 0
    for category_key, gmail_label in targets:
        if created >= limit:
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
            if created >= limit:
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
