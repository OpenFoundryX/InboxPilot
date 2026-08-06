"""Render a meeting recap email.

Plain text, matching the rest of InboxPilot's assistant mail. The transcript is
deliberately not attached — it can be thousands of lines, and it is one API call
away for anyone who wants it.
"""

from core.config import settings
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
    lines.append(_plain(meeting.summary) if meeting.summary else "(no summary available)")

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

    if meeting.has_recording:
        # Deliberately our page, not the provider's download link: that link is
        # signed for a few hours and this email outlives it by years. The page
        # resolves a live one whenever it is opened.
        lines.append("")
        lines.append(f"▶ Watch the recording: {_meeting_link(meeting)}")

    lines.append("")
    lines.append("— InboxOS")
    return subject, "\n".join(lines)


def _plain(summary: str) -> str:
    """Flatten the summary's Markdown for a plain-text email.

    The summary is stored as Markdown so the web app can render headings and
    bullets, but nothing renders it here — a mail client showing literal `##`
    and `-` makes the recap look broken. The same two markers the summarizer
    emits are converted to the bullet style already used for decisions and
    action items below, so the whole email reads as one document.
    """
    out: list[str] = []
    for raw in summary.splitlines():
        line = raw.strip()
        if line.startswith("## "):
            # One blank line before a heading — never a leading one, and never
            # a second on top of the blank the Markdown already carries between
            # blocks, which would double-space the whole email.
            if out and out[-1] != "":
                out.append("")
            out.append(line[3:].strip())
        elif line.startswith("- "):
            out.append(f"  • {line[2:].strip()}")
        else:
            out.append(line)
    return "\n".join(out).strip()


def _meeting_link(meeting: Meeting) -> str:
    return f"{settings.FRONTEND_BASE_URL.rstrip('/')}/dashboard/notetaker/{meeting.id}"
