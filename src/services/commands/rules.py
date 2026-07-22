"""Create a generic Gmail filter from a parsed `create_rule` command."""

from core.logging import get_logger
from integrations.composio.composio_client import get_composio
from services.mailman import gmail_ops

log = get_logger(__name__)

CREATE_FILTER = "GMAIL_CREATE_FILTER"


def create_rule(user_id: str, action: dict) -> str:
    """Build criteria + action and create a Gmail filter. Returns a human summary."""
    crit_in = action.get("criteria") or {}
    criteria: dict = {}
    for key in ("from", "to", "subject", "query"):
        val = crit_in.get(key)
        # Guard against LLM placeholder leakage ("...", dot-only, blank).
        if isinstance(val, str) and val.strip().strip(".").strip():
            criteria[key] = val.strip()
    if not criteria:
        raise ValueError("rule needs at least one criteria field (from/to/subject/query)")

    add_ids: list[str] = []
    remove_ids: list[str] = []
    effects: list[str] = []

    label_name = action.get("apply_label")
    if label_name:
        label_id = gmail_ops.resolve_label_id(user_id, label_name)
        if not label_id:
            raise ValueError(f"label {label_name!r} not found")
        add_ids.append(label_id)
        effects.append(f"label '{label_name}'")
    if action.get("archive"):
        remove_ids.append("INBOX")
        effects.append("skip inbox")
    if action.get("star"):
        add_ids.append("STARRED")
        effects.append("star")
    if action.get("mark_read"):
        remove_ids.append("UNREAD")
        effects.append("mark read")
    if action.get("trash"):
        add_ids.append("TRASH")
        effects.append("trash")

    if not add_ids and not remove_ids:
        raise ValueError("rule needs at least one effect (label/archive/star/mark_read/trash)")

    gmail_action: dict = {}
    if add_ids:
        gmail_action["addLabelIds"] = add_ids
    if remove_ids:
        gmail_action["removeLabelIds"] = remove_ids

    resp = get_composio().tools.execute(
        CREATE_FILTER, {"criteria": criteria, "action": gmail_action}, user_id=user_id
    )
    if resp.get("successful") is False:
        raise RuntimeError(f"Composio {CREATE_FILTER} failed: {resp.get('error')}")

    crit_desc = ", ".join(f"{k}={v}" for k, v in criteria.items())
    log.info("commands.rule_created", user_id=user_id, criteria=criteria)
    return f"Rule: when {crit_desc} → {', '.join(effects)}"
