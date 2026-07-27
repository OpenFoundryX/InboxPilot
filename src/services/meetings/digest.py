"""The meetings section of the daily briefing.

The briefing is otherwise composed straight from labeled mail with no DB access,
so this is a separate function the routine appends rather than a change to
`compose_briefing` — it needs a session, and mail-only digests shouldn't pay for
one.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from services.meetings.store import recent_delivered

MAX_MEETINGS = 5
MAX_ACTIONS_EACH = 3


async def meetings_section(
    db: AsyncSession, user_id: uuid.UUID, *, since_hours: int = 24
) -> str:
    """Return a briefing section for recently summarized meetings, or ""."""
    meetings = await recent_delivered(
        db, user_id, since_hours=since_hours, limit=MAX_MEETINGS
    )
    if not meetings:
        return ""

    lines = ["", f"📝 Meetings ({len(meetings)})"]
    for m in meetings:
        when = m.starts_at.strftime("%a %-I:%M %p") if m.starts_at else ""
        header = f"  • {(m.title or 'Untitled meeting')[:70]}"
        lines.append(f"{header} — {when}" if when else header)
        for item in (m.action_items or [])[:MAX_ACTIONS_EACH]:
            owner = f" ({item['owner']})" if item.get("owner") else ""
            lines.append(f"      ↳ {item.get('what')}{owner}")
    return "\n".join(lines)
