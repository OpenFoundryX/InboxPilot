"""Render a meeting recap email.

Plain text, matching the rest of InboxPilot's assistant mail. The transcript is
deliberately not attached — it can be thousands of lines, and it is one API call
away for anyone who wants it.
"""

from models.meetings import Meeting

MAX_SUBJECT = 120


def compose_recap(meeting: Meeting) -> tuple[str, str]:
    """Return `(subject, body)` for a processed meeting."""
    title = (meeting.title or "your meeting").strip()
    subject = f"📝 Recap: {title}"[:MAX_SUBJECT]

    lines = [f"📝 {title}"]
    if meeting.starts_at:
        lines.append(meeting.starts_at.strftime("%a %d %b, %-I:%M %p UTC"))
    if meeting.attendees:
        lines.append(f"With: {', '.join(meeting.attendees[:10])}")
    lines.append("")
    lines.append(meeting.summary or "(no summary available)")

    if meeting.decisions:
        lines.append("")
        lines.append("✅ Decisions")
        lines.extend(f"  • {d}" for d in meeting.decisions)

    if meeting.action_items:
        lines.append("")
        lines.append("👉 Action items")
        for item in meeting.action_items:
            suffix = []
            if item.get("owner"):
                suffix.append(item["owner"])
            if item.get("due_at"):
                suffix.append(f"due {item['due_at']}")
            tail = f" ({' — '.join(suffix)})" if suffix else ""
            lines.append(f"  • {item.get('what')}{tail}")

    lines.append("")
    lines.append("— InboxOS")
    return subject, "\n".join(lines)
