"""Schedule trusted people — draft slot proposals for VIP meeting requests.

When a trusted (VIP) sender asks to meet, InboxOS finds your free working-hour
slots and drafts a threaded reply proposing them. It only ever creates a draft
(never sends), so you review before it goes out.
"""

import json
import uuid

from openai import OpenAI
from sqlalchemy import select

from core.config import settings
from core.logging import get_logger
from integrations.composio import calendar, gmail
from models.mailman import VipRule
from services.mailman import gmail_ops
from services.mailman.rules import extract_address

log = get_logger(__name__)

SCHEDULED_LABEL = "inboxos-later"  # marks requests we've already drafted for
_SYS = """Does this email ask to schedule/meet/find a time with the recipient? Return ONLY
JSON: {"wants_meeting": bool, "duration_min": <int or null>}."""


def _client() -> OpenAI:
    return OpenAI(api_key=settings.OPENAI_API_KEY)


async def draft_meeting_replies(db, user_id: str, tz: str) -> int:
    """Scan recent VIP mail for meeting requests; draft slot proposals. Returns count."""
    if not calendar.is_connected(user_id):
        return 0
    vip = await db.scalar(select(VipRule).where(VipRule.user_id == uuid.UUID(user_id)))
    senders = list((vip.domains if vip else []) + (vip.addresses if vip else []))
    if not senders:
        return 0

    from_q = " OR ".join(f"from:{s}" for s in senders)
    query = f'({from_q}) newer_than:3d -from:me -label:"{SCHEDULED_LABEL}"'
    emails = gmail.fetch_by_query(user_id, query, 10)

    drafted = 0
    for e in emails:
        if not e.id:
            continue
        content = f"Subject: {e.subject}\n{(e.body or e.snippet or '')[:1200]}"
        try:
            resp = _client().chat.completions.create(
                model=settings.OPENAI_MODEL,
                temperature=0,
                response_format={"type": "json_object"},
                messages=[{"role": "system", "content": _SYS}, {"role": "user", "content": content}],
            )
            data = json.loads(resp.choices[0].message.content or "{}")
        except Exception:
            log.exception("scheduling.detect_failed", message_id=e.id)
            continue
        if not data.get("wants_meeting"):
            continue

        slot_min = int(data.get("duration_min") or 30)
        slots = calendar.free_slots(user_id, tz, days=5, slot_min=slot_min)
        if not slots:
            continue

        requester = extract_address(e.sender) or ""
        body = (
            f"Hi,\n\nHappy to find a time. Here are a few slots that work on my end:\n\n"
            + calendar.format_slots(slots)
            + "\n\nLet me know what suits you and I'll send an invite.\n\nBest"
        )
        subject = f"Re: {e.subject or 'Meeting'}"
        try:
            gmail.create_draft(user_id, requester, subject, body, thread_id=e.thread_id)
            gmail_ops.add_label(user_id, [e.id], SCHEDULED_LABEL)
        except Exception:
            log.exception("scheduling.draft_failed", message_id=e.id)
            continue
        drafted += 1
        log.info("scheduling.drafted", user_id=user_id, requester=requester)
    return drafted
