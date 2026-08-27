"""Schedule trusted people — draft slot proposals for VIP meeting requests.

When a trusted (VIP) sender asks to meet, InboxOS finds your free working-hour
slots and drafts a threaded reply proposing them. It only ever creates a draft
(never sends), so you review before it goes out.
"""

import json
import uuid
from datetime import datetime, timezone

from openai import OpenAI
from sqlalchemy import select

from core.config import settings
from core.logging import get_logger
from integrations.google import calendar, gmail
from models.mailman import VipRule
from services.activity.record import record_draft_created
from services.billing.usage import add_drafts
from services.mailman import gmail_ops
from services.mailman.rules import extract_address

log = get_logger(__name__)

SCHEDULED_LABEL = "inboxos-later"

SYS = """
    Does this email ask to schedule/meet/find a time with the recipient? Return ONLY
    JSON: {"wants_meeting": bool, "duration_min": <int or null>}
"""


def client() -> OpenAI:
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
            resp = client().chat.completions.create(
                model=settings.OPENAI_MODEL,
                temperature=0,
                response_format={"type": "json_object"},
                messages=[{"role": "system", "content": SYS}, {"role": "user", "content": content}],
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
            "Hi,\n\nHappy to find a time. Here are a few slots that work on my end:\n\n"
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
        # Keyed by the source message, not the draft id: one email replied to
        # counts once, even if a re-run leaves a second draft object behind.
        record_draft_created(user_id, e.id)
        # This path is entirely separate from `services.drafts.create` (it
        # calls `gmail.create_draft` directly, so C1's centralized metering
        # there never sees it) and reaches here only through
        # `ROUTINE_SCHEDULE_TRUSTED`, which `routines_sweep.py` already gates
        # on `FEATURE_ROUTINE` before calling in — Starter can't reach this
        # function at all. Metered anyway, for the same reason every other
        # draft-producing path now is: it's cheap, correct, and the dashboard's
        # "drafts used" figure (`UsageOut.drafts_used`) should count every
        # draft InboxOS writes, not just the ones from two of the three
        # producers. Pro's allowance is unlimited today, so this can't cap
        # anyone — it only fixes an under-reported number.
        try:
            await add_drafts(db, uuid.UUID(user_id), 1, datetime.now(timezone.utc))
        except Exception:
            log.warning("scheduling.meter_failed", user_id=user_id)
        drafted += 1
        log.info("scheduling.drafted", user_id=user_id, requester=requester)
    return drafted
