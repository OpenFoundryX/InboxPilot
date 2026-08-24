"""Create a generic Gmail filter from a parsed `create_rule` command.

A rule is a Gmail filter, which only affects *future* mail. When the user asks
to also act on mail already in their mailbox ("archive all current marketing
emails"), the command sets `apply_to_existing`, and we additionally sweep every
matching message and apply the same label changes — mirroring Gmail's
"Also apply filter to matching conversations" checkbox.
"""

from core.logging import get_logger
from integrations.google import gmail
from services.mailman import gmail_ops

log = get_logger(__name__)

# Safety bound for the "apply to existing" sweep. Only ids are read, at 5 Gmail
# quota units per 500, so this stays cheap even at the cap.
_MAX_MATCHES = 2000


def _criteria_to_query(criteria: dict) -> str:
    """Turn filter criteria (from/to/subject/query) into a Gmail search string."""
    parts: list[str] = []
    if criteria.get("from"):
        parts.append(f"from:({criteria['from']})")
    if criteria.get("to"):
        parts.append(f"to:({criteria['to']})")
    if criteria.get("subject"):
        parts.append(f"subject:({criteria['subject']})")
    if criteria.get("query"):
        parts.append(criteria["query"])
    return " ".join(parts).strip()


def _matching_ids(user_id: str, query: str, cap: int = _MAX_MATCHES) -> list[str]:
    """Every message id matching `query` (capped)."""
    return [message_id for message_id, _ in gmail.list_message_ids(user_id, query, cap)]


def _apply_to_existing(user_id: str, query: str, add_ids: list[str], remove_ids: list[str]) -> int:
    """Apply the rule's label changes to all existing matching mail. Returns count."""
    if not query:
        return 0
    ids = _matching_ids(user_id, query)
    if not ids:
        return 0
    gmail.batch_modify(user_id, ids, add=add_ids, remove=remove_ids)
    log.info("commands.rule_applied_existing", user_id=user_id, count=len(ids))
    return len(ids)


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

    gmail.create_filter(user_id, criteria, gmail_action)

    crit_desc = ", ".join(f"{k}={v}" for k, v in criteria.items())
    log.info("commands.rule_created", user_id=user_id, criteria=criteria)
    summary = f"Rule: when {crit_desc} → {', '.join(effects)}"

    # "current and future" → also apply the same effect to mail already present.
    if action.get("apply_to_existing"):
        applied = _apply_to_existing(user_id, _criteria_to_query(criteria), add_ids, remove_ids)
        summary += f"; applied to {applied} existing email(s)"

    return summary
