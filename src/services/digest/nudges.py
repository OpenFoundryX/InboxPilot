"""Follow-up nudges: threads where you're the last sender (awaiting a reply)."""

from core.logging import get_logger
from integrations.google import gmail
from integrations.google.mime import headers
from services.mailman import gmail_ops
from services.notify import send_to_inbox

log = get_logger(__name__)

FOLLOW_UP_LABEL = "to follow up"


def _last_sender(user_id: str, thread_id: str) -> str | None:
    """Who sent the most recent message in a thread.

    Metadata format, requesting only `From`: this runs once per thread across a
    whole sweep, and pulling full bodies to read one header would be the
    expensive way to answer the question.
    """
    thread = gmail.get_thread(user_id, thread_id, metadata_headers=["From"])
    messages = thread.get("messages") or []
    if not messages:
        return None
    return headers(messages[-1].get("payload") or {}).get("from")


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
    send_to_inbox(user_id, email, f"🔔 {len(waiting)} threads awaiting a reply", body)
    log.info("nudges.chase_sent", user_id=user_id, count=len(waiting))
    return len(waiting)


def _address(sender: str | None) -> str | None:
    import re

    if not sender:
        return None
    m = re.search(r"[\w.+-]+@[\w.-]+", sender)
    return m.group(0).lower() if m else None


def reconnect_suggestions(user_id: str, email: str, self_email: str) -> int:
    """Nudge to reach out to real people you emailed a while ago but not since.

    Heuristic: recipients you wrote to 30–120 days ago with no message exchanged
    in the last 30 days.
    """
    old = gmail.fetch_by_query(user_id, "in:sent newer_than:120d older_than:30d", 40)
    recent = gmail.fetch_by_query(user_id, "newer_than:30d", 60)
    recent_people = {a for m in recent for a in [_address(m.sender), _address(m.to)] if a}

    candidates: dict[str, str] = {}  # address -> display subject/context
    for m in old:
        addr = _address(m.to)
        if not addr or addr in recent_people or addr == self_email.lower():
            continue
        # Skip obvious no-reply/automated addresses.
        if any(x in addr for x in ("no-reply", "noreply", "notifications", "mailer")):
            continue
        candidates.setdefault(addr, m.subject or "")

    if not candidates:
        return 0

    lines = [
        f"  • {addr}" + (f' — last about "{ctx[:40]}"' if ctx else "")
        for addr, ctx in list(candidates.items())[:10]
    ]
    body = (
        "It's been a while since you've been in touch with these people — worth reconnecting?\n\n"
        + "\n".join(lines)
        + "\n\n— InboxOS"
    )
    send_to_inbox(user_id, email, f"👋 {len(candidates)} people worth reconnecting with", body)
    log.info("nudges.reconnect_sent", user_id=user_id, count=len(candidates))
    return len(candidates)
