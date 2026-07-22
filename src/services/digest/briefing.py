"""Compose the daily briefing / newsletter digest from already-labeled mail.

Because incoming mail is auto-classified into the org labels, a digest is just a
grouped read of the last day's mail — no extra LLM cost. Actionable buckets are
listed in detail; low-value buckets are summarized as counts.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from integrations.composio import gmail

# Buckets shown in full (with sender/subject), in priority order.
DETAIL_LABELS = ["to do", "to follow up", "fyi", "notification"]
# Buckets shown as a count only.
COUNT_LABELS = ["marketing", "noise"]
ICONS = {
    "to do": "🔴",
    "to follow up": "🟣",
    "fyi": "🟢",
    "notification": "🔵",
    "marketing": "🟡",
    "noise": "⚫",
}


def _line(e) -> str:
    subj = (e.subject or "(no subject)").strip()[:70]
    who = (e.sender or "").split("<")[0].strip()[:30]
    return f"  • {subj}" + (f" — {who}" if who else "")


def compose_briefing(user_id: str, tz: str, window: str = "newer_than:1d") -> tuple[str, str]:
    """Return (subject, body) for a briefing over `window` of received mail."""
    try:
        now_local = datetime.now(ZoneInfo(tz))
    except Exception:
        now_local = datetime.now()

    sections: list[str] = []
    total = 0
    for label in DETAIL_LABELS:
        msgs = gmail.fetch_by_query(user_id, f'label:"{label}" -from:me {window}', 15)
        if not msgs:
            continue
        total += len(msgs)
        header = f"{ICONS.get(label, '•')} {label.title()} ({len(msgs)})"
        sections.append(header + "\n" + "\n".join(_line(m) for m in msgs))

    counts: list[str] = []
    for label in COUNT_LABELS:
        msgs = gmail.fetch_by_query(user_id, f'label:"{label}" -from:me {window}', 30)
        if msgs:
            total += len(msgs)
            counts.append(f"{ICONS.get(label, '•')} {label.title()}: {len(msgs)} (hidden)")

    if now_local.hour < 12:
        greeting = "Good morning"
    elif now_local.hour < 17:
        greeting = "Good afternoon"
    else:
        greeting = "Good evening"
    date_str = now_local.strftime("%A, %d %b")
    subject = f"☀️ Briefing — {date_str}"

    if total == 0:
        body = f"{greeting}! Nothing new to brief you on for {date_str}. Enjoy the calm.\n\n— InboxOS"
        return subject, body

    parts = [f"{greeting}! Here's what landed since yesterday ({date_str}):", ""]
    for section in sections:
        parts += [section, ""]
    if counts:
        parts += ["Filtered out:", *counts, ""]
    parts.append("— InboxOS")
    return subject, "\n".join(parts)
