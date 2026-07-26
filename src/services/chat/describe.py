"""Turn a parsed action dict into something a human can approve.

The confirm card must state plainly what will happen — especially for the
destructive effects (trash, archive, apply-to-existing). Wording here is the
last thing a user reads before an action runs, so it names the effect rather
than the action type.
"""


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
        effects.append(f"label “{action['apply_label']}”")
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
    bits = []
    for key in (
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
    ):
        if action.get(key) is not None:
            bits.append(f"{key} = {action[key]}")
    return ", ".join(bits)


def describe_action(action: dict) -> dict:
    """Return {"type", "label", "detail"} for one proposed action."""
    atype = action.get("type") or "unknown"
    label = atype
    detail = ""

    if atype == "create_label":
        label = f"Create Gmail label “{action.get('name', '')}”"
    elif atype == "delete_label":
        label = f"Delete Gmail label “{action.get('name', '')}”"
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
        routine = action.get("routine") or "briefing"
        state = "Turn off" if action.get("enabled") is False else "Turn on"
        label = f"{state} the “{routine}” routine"
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
        label = f"Set a reminder: “{action.get('title') or 'Reminder'}”"
        detail = f"At {action.get('remind_at', '')}"

    return {"type": atype, "label": label, "detail": detail}


def describe_actions(actions: list[dict]) -> list[dict]:
    return [describe_action(a) for a in actions]
