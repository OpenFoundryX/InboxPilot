"""Turn a parsed action dict into something a human can approve.

The confirm card must state plainly what will happen — especially for the
destructive effects (trash, archive, apply-to-existing). Wording here is the
last thing a user reads before an action runs, so it names the effect rather
than the action type.
"""

from models.routines import (
    ROUTINE_BRIEFING,
    ROUTINE_CATCHUP,
    ROUTINE_CHASE_THREADS,
    ROUTINE_DEADLINE_SCAN,
    ROUTINE_DOUBLE_BOOKINGS,
    ROUTINE_INVOICES,
    ROUTINE_NEWSLETTER_DIGEST,
    ROUTINE_RECONNECT,
    ROUTINE_SCHEDULE_TRUSTED,
)

# Map routine slugs to human-readable names
_ROUTINE_NAMES = {
    ROUTINE_BRIEFING: "your daily briefing",
    ROUTINE_NEWSLETTER_DIGEST: "your newsletter digest",
    ROUTINE_CHASE_THREADS: "nudges for threads awaiting a reply",
    ROUTINE_RECONNECT: "reconnect nudges",
    ROUTINE_DEADLINE_SCAN: "the deadline scan",
    ROUTINE_CATCHUP: "your catch-up on unread mail",
    ROUTINE_INVOICES: "your invoice summary",
    ROUTINE_DOUBLE_BOOKINGS: "the calendar clash heads-up",
    ROUTINE_SCHEDULE_TRUSTED: "draft meeting times for VIP requests",
}

# Typographic quotes
_OPEN_QUOTE = "“"
_CLOSE_QUOTE = "”"


def _joined(action: dict, *keys: str) -> str:
    parts = []
    for key in keys:
        values = [str(v) for v in (action.get(key) or [])]
        if values:
            parts.append(f"{key}: {', '.join(values)}")
    return "; ".join(parts)


def _rule_detail(action: dict) -> str:
    criteria = action.get("criteria") or {}
    crit = ", ".join(f"{k}={v}" for k, v in criteria.items() if v) or "any mail"
    effects = []
    if action.get("apply_label"):
        label_name = action["apply_label"]
        effects.append(f"label {_OPEN_QUOTE}{label_name}{_CLOSE_QUOTE}")
    if action.get("archive"):
        effects.append("skip the inbox")
    if action.get("trash"):
        effects.append("move to trash")
    if action.get("star"):
        effects.append("star")
    if action.get("mark_read"):
        effects.append("mark as read")
    detail = f"Matching {crit} → {', '.join(effects) or 'no effect'}"
    if action.get("apply_to_existing"):
        detail += ". Also applies to existing mail already in the mailbox"
    return detail


def _routine_detail(action: dict) -> str:
    phrases = []

    # Delivery frequency
    delivery_mode = action.get("delivery_mode")
    times_per_day = action.get("times_per_day")
    interval_minutes = action.get("interval_minutes")
    interval_hours = action.get("interval_hours")
    custom_times = action.get("custom_times")

    if delivery_mode == "times_per_day" and times_per_day is not None:
        phrase = (
            f"Delivers {times_per_day} times a day" if times_per_day != 1 else "Delivers once a day"
        )
        phrases.append(phrase)
    elif delivery_mode == "custom_daily" and custom_times:
        # custom_times is a list like ['13:00', '18:00']
        times = [str(t) for t in custom_times]
        if len(times) == 1:
            phrases.append(f"Delivers at {times[0]}")
        elif len(times) == 2:
            phrases.append(f"Delivers at {times[0]} and {times[1]}")
        else:
            joined = ", ".join(times[:-1]) + f" and {times[-1]}"
            phrases.append(f"Delivers at {joined}")
    elif interval_minutes is not None:
        unit = "minute" if interval_minutes == 1 else "minutes"
        phrases.append(f"Delivers every {interval_minutes} {unit}")
    elif interval_hours is not None:
        unit = "hour" if interval_hours == 1 else "hours"
        phrases.append(f"Delivers every {interval_hours} {unit}")

    # Active window
    active_window_start = action.get("active_window_start")
    active_window_end = action.get("active_window_end")
    if active_window_start is not None and active_window_end is not None:
        phrases.append(f"Active {active_window_start}–{active_window_end}")

    # Do-not-disturb
    dnd_enabled = action.get("dnd_enabled")
    if dnd_enabled is not None:
        if dnd_enabled:
            dnd_start = action.get("dnd_start")
            dnd_end = action.get("dnd_end")
            if dnd_start is not None and dnd_end is not None:
                phrases.append(f"Quiet hours {dnd_start}–{dnd_end}")
            else:
                phrases.append("Quiet hours on")
        else:
            phrases.append("Quiet hours off")

    # Timezone
    timezone = action.get("timezone")
    if timezone is not None:
        phrases.append(f"Timezone {timezone}")

    return ". ".join(phrases)


def describe_action(action: dict) -> dict:
    """Return {"type", "label", "detail"} for one proposed action."""
    atype = action.get("type") or "unknown"
    label = atype
    detail = ""

    if atype == "create_label":
        name = action.get("name", "")
        label = f"Create Gmail label {_OPEN_QUOTE}{name}{_CLOSE_QUOTE}"
    elif atype == "delete_label":
        name = action.get("name", "")
        label = f"Delete Gmail label {_OPEN_QUOTE}{name}{_CLOSE_QUOTE}"
        detail = "The label is removed from every message that has it"
    elif atype == "set_routine":
        label = "Change your delivery routine"
        detail = _routine_detail(action)
    elif atype == "add_vip":
        label = "Add to your VIP list"
        detail = _joined(action, "domains", "addresses", "keywords")
    elif atype == "remove_vip":
        label = "Remove from your VIP list"
        detail = _joined(action, "domains", "addresses", "keywords")
    elif atype == "create_rule":
        label = "Create a Gmail rule"
        detail = _rule_detail(action)
    elif atype == "manage_routine":
        routine_slug = action.get("routine") or ROUTINE_BRIEFING
        routine_name = _ROUTINE_NAMES.get(routine_slug, routine_slug)
        state = "Turn off" if action.get("enabled") is False else "Turn on"
        label = f"{state} {routine_name}"
        if action.get("run_time"):
            detail = f"Runs at {action['run_time']}"
    elif atype == "send_briefing_now":
        label = "Send your briefing now"
    elif atype == "catch_up_now":
        label = "Send a catch-up summary now"
    elif atype == "summarize_invoices_now":
        label = "Send an invoice summary now"
    elif atype == "scan_deadlines_now":
        label = "Scan recent mail for deadlines"
        detail = "Creates reminders for anything it finds"
    elif atype == "set_reminder":
        title = action.get("title") or "Reminder"
        label = f"Set a reminder: {_OPEN_QUOTE}{title}{_CLOSE_QUOTE}"
        detail = f"At {action.get('remind_at', '')}"

    return {"type": atype, "label": label, "detail": detail}


def describe_actions(actions: list[dict]) -> list[dict]:
    return [describe_action(a) for a in actions]
