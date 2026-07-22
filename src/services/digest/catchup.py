"""Catch what you skipped — scan recent unread and surface what mattered."""

from integrations.composio import gmail
from services.classify.classifier import LABEL_NAMES

# Order matters: most important first.
PRIORITY = ["to do", "to follow up", "fyi", "notification"]


def compose_catchup(user_id: str, days: int = 30) -> tuple[str, str]:
    """Return (subject, body) summarizing important unread mail from the last N days."""
    subject = f"📥 Catch-up — unread worth a look ({days}d)"
    sections: list[str] = []
    total = 0
    for label in PRIORITY:
        msgs = gmail.fetch_by_query(
            user_id, f'is:unread label:"{label}" -from:me newer_than:{days}d', 15
        )
        if not msgs:
            continue
        total += len(msgs)
        lines = [
            f"  • {(m.subject or '(no subject)')[:70]} — {(m.sender or '').split('<')[0].strip()[:30]}"
            for m in msgs
        ]
        sections.append(f"{label.title()} ({len(msgs)})\n" + "\n".join(lines))

    if total == 0:
        return subject, "You're all caught up — nothing important sitting unread. 🎉\n\n— InboxOS"

    parts = [
        f"Here's the unread that looks worth your time (last {days} days):",
        "",
        *[s + "\n" for s in sections],
        "(Marketing/noise unread left out on purpose.)",
        "",
        "— InboxOS",
    ]
    return subject, "\n".join(parts)
