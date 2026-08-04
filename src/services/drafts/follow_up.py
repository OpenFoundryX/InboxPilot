"""Find threads the user spoke last in that went quiet, and draft a nudge.

Deciding "no reply came" without a per-thread Gmail fetch is the whole problem
here. The approach: pull the user's sent mail from the window, pull everything
received since, and compare timestamps per thread. If the newest inbound message
in a thread predates the user's own last message, the user is the one waiting.

That comparison is what makes the feature useful. The cheaper filter — "skip any
thread containing inbound mail" — would exclude the single most common case
(they wrote, you answered, now you are waiting), so it is not enough.
"""

from datetime import UTC, datetime

from core.logging import get_logger
from integrations.composio import gmail
from schemas.email import EmailSummary
from services.drafts.context import DraftConfig
from services.drafts.create import draft_follow_up
from services.drafts.sweep import effective_limit

log = get_logger(__name__)

# How far back to look for sent mail worth chasing. Beyond this a nudge reads as
# out of the blue rather than a follow-up.
LOOKBACK_EXTRA_DAYS = 14
# Candidates pulled per user per sweep.
MAX_CANDIDATES = 25
# Drafts actually created per user per sweep. Each one is an LLM call plus a
# Gmail write; a user who enables this on a busy mailbox should not get twenty
# nudges appearing at once.
MAX_PER_SWEEP = 5


def _as_utc(value: datetime | None) -> datetime | None:
    """Gmail dates arrive tz-aware, but a naive one would crash the comparison."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _latest_inbound_by_thread(emails: list[EmailSummary]) -> dict[str, datetime]:
    latest: dict[str, datetime] = {}
    for e in emails:
        when = _as_utc(e.date)
        if not e.thread_id or when is None:
            continue
        current = latest.get(e.thread_id)
        if current is None or when > current:
            latest[e.thread_id] = when
    return latest


def find_quiet_threads(
    user_id: str, days: int
) -> list[tuple[EmailSummary, int]]:
    """Sent messages awaiting a reply, newest first, paired with days waited."""
    # `-label:DRAFTED_LABEL` keeps a thread from being nudged again every day.
    # Nothing about drafts is stored, so this exclusion is the only record that
    # this sent message has already had its follow-up written.
    sent = gmail.fetch_by_query(
        user_id,
        f"in:sent older_than:{days}d newer_than:{days + LOOKBACK_EXTRA_DAYS}d "
        f'-label:"{gmail.DRAFTED_LABEL}"',
        MAX_CANDIDATES,
        verbose=True,
    )
    if not sent:
        return []

    # Everything inbound across the same window. One query, not one per thread.
    inbound = gmail.fetch_by_query(
        user_id,
        f"-in:sent newer_than:{days + LOOKBACK_EXTRA_DAYS}d",
        None,
    )
    latest_inbound = _latest_inbound_by_thread(inbound)

    now = datetime.now(UTC)
    quiet: list[tuple[EmailSummary, int]] = []
    # Keep only the newest sent message per thread: a thread we wrote to three
    # times needs one nudge, hung off the last thing we said.
    seen_threads: set[str] = set()

    for e in sorted(sent, key=lambda m: _as_utc(m.date) or now, reverse=True):
        sent_at = _as_utc(e.date)
        if not e.id or sent_at is None:
            continue
        if e.thread_id:
            if e.thread_id in seen_threads:
                continue
            seen_threads.add(e.thread_id)
            reply_at = latest_inbound.get(e.thread_id)
            if reply_at is not None and reply_at > sent_at:
                continue
        quiet.append((e, max(0, (now - sent_at).days)))

    return quiet


def sweep_user(
    user_id: str,
    config: DraftConfig,
    user_name: str | None = None,
    *,
    budget: int | None = None,
) -> int:
    """Draft follow-ups for one user. Returns how many were created.

    `budget` is the user's remaining monthly draft quota (see
    `workers.jobs.drafts_sweep.remaining_drafts`); without capping against it,
    a user near their limit could take a full `MAX_PER_SWEEP` batch of nudges
    and land over quota, the same exactness problem `sweep.sweep_user` guards
    against. `effective_limit` is reused from there, but with this module's
    own `MAX_PER_SWEEP` (5, not `sweep.py`'s 10) passed explicitly.
    """
    if not config.follow_up_enabled:
        return 0

    limit = effective_limit(budget, MAX_PER_SWEEP)
    if limit <= 0:
        return 0

    candidates = find_quiet_threads(user_id, config.follow_up_days)
    if not candidates:
        return 0

    created = 0
    for email, days_quiet in candidates:
        if created >= limit:
            break
        try:
            draft_id = draft_follow_up(
                user_id,
                message_id=email.id or "",
                recipient_raw=email.to,
                subject=email.subject,
                body=email.body or email.snippet,
                days_quiet=days_quiet,
                thread_id=email.thread_id,
                user_name=user_name,
                config=config,
            )
        except Exception:
            # One bad thread must not end the sweep for the rest.
            log.exception("drafts.follow_up_failed", user_id=user_id, message_id=email.id)
            continue
        if draft_id:
            created += 1

    if created:
        log.info("drafts.follow_ups_created", user_id=user_id, count=created)
    return created
