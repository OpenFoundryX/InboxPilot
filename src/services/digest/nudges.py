"""Follow-up nudges: threads where you're the last sender (awaiting a reply)."""

from core.logging import get_logger
from integrations.composio import gmail
from integrations.composio.composio_client import get_composio
from services.mailman import gmail_ops

log = get_logger(__name__)

FOLLOW_UP_LABEL = "to follow up"


def _last_sender(user_id: str, thread_id: str) -> str | None:
    resp = get_composio().tools.execute(
        "GMAIL_FETCH_MESSAGE_BY_THREAD_ID", {"thread_id": thread_id}, user_id=user_id
    )
    msgs = (resp.get("data") or {}).get("messages") or []
    return (msgs[-1].get("sender") if msgs else None)


def chase_open_threads(user_id: str, email: str, self_email: str) -> int:
    """Label + nudge threads sent 2–7 days ago that have had no reply.

    Returns the number of waiting threads found.
    """
    sent = gmail.fetch_by_query(user_id, "in:sent newer_than:7d older_than:2d", 25)
    waiting: list[tuple[str, str]] = []  # (subject, message_id)
    seen: set[str] = set()

    for m in sent:
        tid = m.thread_id
        if not tid or tid in seen:
            continue
        seen.add(tid)
        try:
            last = _last_sender(user_id, tid) or ""
        except Exception:
            continue
        # If the most recent message is still from the user, they're waiting.
        if self_email.lower() in last.lower():
            waiting.append((m.subject or "(no subject)", m.id))

    if not waiting:
        return 0

    ids = [mid for _, mid in waiting if mid]
    if ids:
        try:
            gmail_ops.add_label(user_id, ids, FOLLOW_UP_LABEL)
        except Exception:
            log.warning("nudges.label_failed", user_id=user_id, exc_info=True)

    lines = [f"  • {subj[:70]}" for subj, _ in waiting]
    body = (
        "You're still waiting on replies to these — want to nudge them?\n\n"
        + "\n".join(lines)
        + "\n\n(I've labeled them 'to follow up'.)\n\n— InboxOS"
    )
    gmail.send_email(user_id, email, f"🔔 {len(waiting)} threads awaiting a reply", body)
    log.info("nudges.chase_sent", user_id=user_id, count=len(waiting))
    return len(waiting)
