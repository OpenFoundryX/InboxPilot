"""Create one draft, end to end. The single path both callers use.

The arrival job (mail landing now) and the catch-up sweep both come through here,
the same way `services.classify.apply` is shared by the webhook task and the
onboarding backfill.

Nothing about the draft is stored. The only trace is `gmail.DRAFTED_LABEL` on the
source message, and that marker is load-bearing: the sweeps exclude it, so it is
the sole reason a 15-minute catch-up pass does not re-draft the same email every
time it runs. It is applied immediately after the draft is created, and a failure
to apply it is escalated rather than swallowed — an unmarked draft would be
recreated on the next pass.
"""

from functools import lru_cache

from core.logging import get_logger
from integrations.composio import gmail
from models.drafts import KIND_FOLLOW_UP, KIND_REPLY
from services.activity.record import record_draft_created
from services.drafts.context import DraftConfig, get_config
from services.drafts.generate import generate_follow_up, generate_reply
from services.mailman import gmail_ops
from services.mailman.rules import extract_address

log = get_logger(__name__)


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

    recipient = extract_address(sender)
    if not recipient:
        log.info("drafts.no_recipient", user_id=user_id, message_id=message_id, sender=sender)
        return None

    draft = generate_reply(
        config,
        sender=sender,
        subject=subject,
        body=body,
        thread_excerpt=thread_excerpt,
        user_name=user_name,
    )
    if not draft.should_draft:
        log.info("drafts.declined", user_id=user_id, message_id=message_id, reason=draft.reason)
        # Marked even though nothing was drafted. The decision is deterministic
        # enough that re-asking every 15 minutes would just spend the same tokens
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
