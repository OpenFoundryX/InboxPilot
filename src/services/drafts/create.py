"""Create one draft, end to end. The single path both callers use.

Both producers come through here — the scheduled draft pass and the follow-up
sweep — the same way `services.classify.apply` is shared by the webhook task and
the onboarding backfill. There used to be a third, chained off classification so
a draft appeared the moment mail landed; drafting is scheduled now, so nothing
writes a draft outside a pass.

This is also the single choke point for billing on drafts: the entitlement gate
(`_entitled_for_draft`) and the usage meter (`_meter_draft`) both live here,
in `_create_and_mark`, rather than in the callers. Gating at the call sites
would need a copy per caller kept in sync, and the arrival path that used to
exist had none at all until this was centralized — a locked or never-subscribed
account generated AI drafts forever, and every arrival draft went uncounted
against the monthly cap. `services/commands/
handlers.py:149` gates `_now` routine actions the same way, for the same
reason: one place a future caller can't bypass.

Nothing about the draft is stored. The only trace is `gmail.DRAFTED_LABEL` on the
source message, and that marker is load-bearing: the sweeps exclude it, so it is
the sole reason a scheduled pass does not re-draft the same email every time it
runs. It is applied immediately after the draft is created, and a failure
to apply it is escalated rather than swallowed — an unmarked draft would be
recreated on the next pass.
"""

import uuid
from datetime import datetime, timezone
from functools import lru_cache

from core.database import run_async, with_worker_session
from core.logging import get_logger
from integrations.composio import gmail
from models.drafts import KIND_FOLLOW_UP, KIND_REPLY
from services.activity.record import record_draft_created
from services.billing.entitlements import FEATURE_DRAFT, Decision, check
from services.billing.usage import add_drafts
from services.drafts.context import DraftConfig, get_config
from services.drafts.generate import generate_follow_up, generate_reply
from services.mailman import gmail_ops
from services.mailman.rules import extract_address

log = get_logger(__name__)


def _entitled_for_draft(user_id: str) -> bool:
    """Whether this user's plan still allows one more generated draft, checked
    live at the moment of creation.

    This is the single gate every draft-producing caller passes through
    (`drafts.sweep`'s scheduled pass and `drafts.follow_up`'s nudges) — a locked
    account, or one that has exhausted its monthly cap, stops here regardless
    of which called in.
    Checked *before* `generate_reply`/`generate_follow_up` so a denied draft
    never spends the LLM call it would otherwise need.

    Denial is the ordinary "no draft" outcome the callers already handle
    (drafting off, wrong category, model declined), not an error — matching
    the spec: quota exhaustion must never surface as a failure to the user.
    """
    uid = uuid.UUID(user_id)
    now = datetime.now(timezone.utc)
    decision: Decision = run_async(
        with_worker_session(lambda db: check(db, uid, FEATURE_DRAFT, now=now))
    )
    return decision.allowed


def _meter_draft(user_id: str) -> None:
    """Count one draft against the user's monthly quota.

    Called exactly once per draft actually created, from `_create_and_mark` —
    the single funnel both `draft_reply` and `draft_follow_up` use — so a
    draft can be metered at most once no matter which caller produced it.
    `drafts.sweep`/`drafts.follow_up` used to call `add_drafts` themselves in
    a batch after their loop; that is removed now that every draft counts
    itself here, so the two are not double-counted.

    Best-effort, like `record_draft_created` just above: the Gmail draft and
    its `DRAFTED_LABEL` marker are already committed by this point, so a
    raised exception here would only make the caller's autoretry run
    `draft_reply` again for the same message, spending a second LLM call and
    creating a second draft. Losing a count on a rare failure is the far
    smaller error. (This was sharper when a per-message arrival task retried
    on any exception; the sweep re-queries and finds the marker instead.)
    """
    uid = uuid.UUID(user_id)
    now = datetime.now(timezone.utc)
    try:
        run_async(with_worker_session(lambda db: add_drafts(db, uid, 1, now)))
    except Exception:
        log.warning("drafts.meter_failed", user_id=user_id)


@lru_cache(maxsize=256)
def _ensure_labels_once(user_id: str) -> bool:
    """Provision the InboxOS labels for a user, at most once per process.

    Mirrors `services.classify.apply._ensure_labels_once`, and it is needed
    separately here because the sweeps never go through classification. Without
    it, `DRAFTED_LABEL` would not exist for any account provisioned before this
    feature shipped, `gmail_ops.add_label` would raise, and the marker would
    never land — so every sweep would draft the same email again. One LIST plus
    creates for whatever is missing; a raised error is not cached, so it retries.
    """
    gmail.ensure_labels(user_id)
    return True


def _mark_drafted(user_id: str, message_id: str) -> None:
    """Apply the marker that stops this message being drafted for again."""
    _ensure_labels_once(user_id)
    gmail_ops.add_label(user_id, [message_id], gmail.DRAFTED_LABEL)


def reply_subject(subject: str | None) -> str:
    """`Re:` the subject, without stacking a second one on an existing reply."""
    subject = (subject or "").strip()
    if not subject:
        return "Re:"
    if subject[:3].casefold() == "re:":
        return subject
    return f"Re: {subject}"


def _create_and_mark(
    *,
    user_id: str,
    source_message_id: str,
    thread_id: str | None,
    kind: str,
    recipient: str,
    subject: str,
    body: str,
) -> str | None:
    """Create the Gmail draft, then mark the source message as drafted."""
    gmail_draft_id = gmail.create_draft(user_id, recipient, subject, body, thread_id=thread_id)

    # Not in a try/except that swallows: if the marker fails to land, the sweep
    # will draft this message again on its next pass. Letting the task fail (and
    # be retried) is the lesser problem, because a retry that finds the marker
    # already applied stops at the query filter.
    _mark_drafted(user_id, source_message_id)

    # Keyed by the source message, not the draft id: one email replied to counts
    # once, matching how `digest.scheduling` records its drafts. Best-effort —
    # this is a dashboard counter, and losing one must not fail the task and
    # trigger a retry that drafts a second time.
    try:
        record_draft_created(user_id, source_message_id)
    except Exception:
        log.warning("drafts.activity_record_failed", user_id=user_id, message_id=source_message_id)

    # Every draft funnels through here, so this is the one place that counts
    # against the monthly cap — see `_meter_draft`'s docstring for why the
    # sweeps no longer do this themselves.
    _meter_draft(user_id)

    log.info(
        "drafts.created",
        user_id=user_id,
        message_id=source_message_id,
        kind=kind,
        gmail_draft_id=gmail_draft_id,
    )
    return gmail_draft_id


def draft_reply(
    user_id: str,
    *,
    message_id: str,
    sender: str | None,
    subject: str | None,
    body: str | None,
    to: str | None = None,
    cc: str | None = None,
    thread_id: str | None = None,
    category_key: str | None = None,
    thread_excerpt: str | None = None,
    user_name: str | None = None,
    config: DraftConfig | None = None,
) -> str | None:
    """Draft a reply to one incoming email. Returns the Gmail draft id, or None.

    None is the ordinary outcome, not an error: drafting is off, the category is
    not one the user picked, or the model judged that this email does not warrant
    a reply.

    `config` is a parameter so a sweep can load it once for a whole batch instead
    of re-reading it per message.
    """
    config = config or get_config(user_id)
    if not config.is_enabled:
        return None
    if not config.drafts_for(category_key):
        return None
    if not _entitled_for_draft(user_id):
        log.info("drafts.no_entitlement", user_id=user_id, message_id=message_id)
        return None

    recipient = extract_address(sender)
    if not recipient:
        log.info("drafts.no_recipient", user_id=user_id, message_id=message_id, sender=sender)
        return None

    draft = generate_reply(
        config,
        sender=sender,
        subject=subject,
        body=body,
        to=to,
        cc=cc,
        thread_excerpt=thread_excerpt,
        user_name=user_name,
    )
    if not draft.should_draft:
        log.info("drafts.declined", user_id=user_id, message_id=message_id, reason=draft.reason)
        # Marked even though nothing was drafted. The decision is deterministic
        # enough that re-asking on every pass would just spend the same tokens
        # to decline again — and on the strictest selectivity setting, declining
        # is the common case.
        _mark_drafted(user_id, message_id)
        return None

    return _create_and_mark(
        user_id=user_id,
        source_message_id=message_id,
        thread_id=thread_id,
        kind=KIND_REPLY,
        recipient=recipient,
        subject=reply_subject(subject),
        body=draft.body,
    )


def draft_follow_up(
    user_id: str,
    *,
    message_id: str,
    recipient_raw: str | None,
    subject: str | None,
    body: str | None,
    days_quiet: int,
    thread_id: str | None = None,
    user_name: str | None = None,
    config: DraftConfig | None = None,
) -> str | None:
    """Draft a nudge on a thread of the user's that went unanswered."""
    config = config or get_config(user_id)
    if not config.is_enabled or not config.follow_up_enabled:
        return None
    if not _entitled_for_draft(user_id):
        log.info("drafts.no_entitlement", user_id=user_id, message_id=message_id)
        return None

    recipient = extract_address(recipient_raw)
    if not recipient:
        return None

    draft = generate_follow_up(
        config,
        recipient=recipient,
        subject=subject,
        body=body,
        days_quiet=days_quiet,
        user_name=user_name,
    )
    if not draft.should_draft:
        _mark_drafted(user_id, message_id)
        return None

    return _create_and_mark(
        user_id=user_id,
        source_message_id=message_id,
        thread_id=thread_id,
        kind=KIND_FOLLOW_UP,
        recipient=recipient,
        subject=reply_subject(subject),
        body=draft.body,
    )
