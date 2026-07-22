"""Execute parsed command actions against InboxPilot's services.

Each handler is sync/blocking (Composio + a SQLAlchemy session passed in) and
returns a short human-readable result line for the confirmation email. Handlers
raise on failure; the caller turns that into a "failed: ..." line.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.logging import get_logger
from integrations.composio import gmail
from datetime import datetime, timezone

from models.mailman import MODE_CUSTOM_DAILY, MODE_INTERVAL, MODE_TIMES
from models.reminders import ORIGIN_MANUAL, Reminder
from models.routines import ROUTINE_BRIEFING, Routine
from models.users import User
from services.commands import rules
from services.digest.briefing import compose_briefing
from services.mailman import filters
from services.mailman.store import get_or_create_settings, get_or_create_vip
from services.notify import send_to_inbox

log = get_logger(__name__)

_MODES = {MODE_INTERVAL, MODE_TIMES, MODE_CUSTOM_DAILY}
_ROUTINE_FIELDS = {
    "delivery_mode",
    "interval_minutes",
    "interval_hours",
    "times_per_day",
    "custom_times",
    "active_window_start",
    "active_window_end",
    "dnd_enabled",
    "dnd_start",
    "dnd_end",
    "timezone",
}


def _merge(existing: list, incoming: list | None) -> list:
    out = list(existing)
    for x in incoming or []:
        if x and x not in out:
            out.append(x)
    return out


def _subtract(existing: list, incoming: list | None) -> list:
    drop = {x for x in (incoming or [])}
    return [x for x in existing if x not in drop]


async def execute(db: AsyncSession, uid: uuid.UUID, action: dict) -> str:
    """Run one action. `uid` is the app user id == Composio user_id."""
    user_id = str(uid)
    atype = action.get("type")

    if atype == "create_label":
        name = (action.get("name") or "").strip()
        if not name:
            raise ValueError("create_label needs a name")
        gmail.create_label(user_id, name)
        return f"Created label '{name}'"

    if atype == "delete_label":
        name = (action.get("name") or "").strip()
        removed = gmail.delete_label(user_id, name)
        return f"Deleted label '{name}'" if removed else f"Label '{name}' not found"

    if atype == "set_routine":
        settings = await get_or_create_settings(db, uid)
        applied = {}
        for field in _ROUTINE_FIELDS:
            if field in action and action[field] is not None:
                if field == "delivery_mode" and action[field] not in _MODES:
                    raise ValueError(f"delivery_mode must be one of {sorted(_MODES)}")
                setattr(settings, field, action[field])
                applied[field] = action[field]
        return f"Updated routine: {applied}" if applied else "Routine: nothing to change"

    if atype in ("add_vip", "remove_vip"):
        vip = await get_or_create_vip(db, uid)
        op = _merge if atype == "add_vip" else _subtract
        vip.domains = op(vip.domains, action.get("domains"))
        vip.addresses = op(vip.addresses, action.get("addresses"))
        vip.keywords = op(vip.keywords, action.get("keywords"))
        settings = await get_or_create_settings(db, uid)
        if settings.is_active:
            # Rebuild the live hold filter so VIP changes take effect now.
            settings.gmail_filter_id = filters.apply_hold_filter(
                user_id,
                domains=vip.domains,
                addresses=vip.addresses,
                keywords=vip.keywords,
                existing_filter_id=settings.gmail_filter_id,
            )
        verb = "Added to" if atype == "add_vip" else "Removed from"
        return f"{verb} VIP (domains={vip.domains}, addresses={vip.addresses}, keywords={vip.keywords})"

    if atype == "create_rule":
        return rules.create_rule(user_id, action)

    if atype == "manage_routine":
        rtype = action.get("routine") or ROUTINE_BRIEFING
        row = await db.scalar(
            select(Routine).where(Routine.user_id == uid, Routine.type == rtype)
        )
        if row is None:
            row = Routine(user_id=uid, type=rtype)
            db.add(row)
        if "enabled" in action and action["enabled"] is not None:
            row.enabled = bool(action["enabled"])
        if action.get("run_time"):
            row.run_time = action["run_time"]
        if "weekday" in action:
            row.weekday = action["weekday"]
        state = "on" if row.enabled else "off"
        when = f"at {row.run_time}" + ("" if row.weekday is None else f" (weekday {row.weekday})")
        return f"Routine '{rtype}' {state} {when}"

    if atype == "send_briefing_now":
        settings = await get_or_create_settings(db, uid)
        user = await db.get(User, uid)
        subject, body = compose_briefing(user_id, settings.timezone)
        send_to_inbox(user_id, user.email, subject, body)
        return "Sent your briefing"

    if atype == "set_reminder":
        raw = action.get("remind_at")
        if not raw:
            raise ValueError("set_reminder needs a remind_at time")
        try:
            remind_at = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"could not parse remind_at {raw!r}") from exc
        if remind_at.tzinfo is None:
            remind_at = remind_at.replace(tzinfo=timezone.utc)
        title = (action.get("title") or "Reminder").strip()
        db.add(
            Reminder(
                user_id=uid,
                remind_at=remind_at,
                title=title,
                note=action.get("note"),
                origin=ORIGIN_MANUAL,
            )
        )
        return f"Reminder set: '{title}' at {remind_at.isoformat()}"

    raise ValueError(f"unknown action type: {atype!r}")
