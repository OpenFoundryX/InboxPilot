"""Create a generic Gmail filter from a parsed `create_rule` command.

A rule is a Gmail filter, which only affects *future* mail. When the user asks
to also act on mail already in their mailbox ("archive all current marketing
emails"), the command sets `apply_to_existing`, and we additionally sweep every
matching message and apply the same label changes — mirroring Gmail's
"Also apply filter to matching conversations" checkbox.
"""

from core.logging import get_logger
from integrations.composio.composio_client import get_composio
from services.mailman import gmail_ops

log = get_logger(__name__)

CREATE_FILTER = "GMAIL_CREATE_FILTER"
FETCH_EMAILS = "GMAIL_FETCH_EMAILS"
BATCH_MODIFY = "GMAIL_BATCH_MODIFY_MESSAGES"

# Safety bounds for the "apply to existing" sweep.
_MAX_MATCHES = 2000
_PAGE = 100
_BATCH = 500


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
    """Page through every message matching `query`; return their ids (capped)."""
    client = get_composio()
    ids: list[str] = []
    token: str | None = None
    while len(ids) < cap:
        payload: dict = {"query": query, "max_results": _PAGE, "ids_only": True}
        if token:
            payload["page_token"] = token
        resp = client.tools.execute(FETCH_EMAILS, payload, user_id=user_id)
        if resp.get("successful") is False:
            break
        data = resp.get("data") or {}
        for m in data.get("messages") or []:
            mid = m.get("messageId") or m.get("id")
            if mid:
                ids.append(mid)
        token = data.get("nextPageToken") or data.get("next_page_token")
        if not token:
            break
    return ids[:cap]


def _apply_to_existing(
    user_id: str, query: str, add_ids: list[str], remove_ids: list[str]
) -> int:
    """Apply the rule's label changes to all existing matching mail. Returns count."""
    if not query:
        return 0
    ids = _matching_ids(user_id, query)
    client = get_composio()
    applied = 0
    for i in range(0, len(ids), _BATCH):
        chunk = ids[i : i + _BATCH]
        resp = client.tools.execute(
            BATCH_MODIFY,
            {"messageIds": chunk, "addLabelIds": add_ids, "removeLabelIds": remove_ids},
            user_id=user_id,
        )
        if resp.get("successful") is False:
            raise RuntimeError(f"Composio {BATCH_MODIFY} failed: {resp.get('error')}")
        applied += len(chunk)
    log.info("commands.rule_applied_existing", user_id=user_id, count=applied)
    return applied


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
    summary = f"Rule: when {crit_desc} → {', '.join(effects)}"

    # "current and future" → also apply the same effect to mail already present.
    if action.get("apply_to_existing"):
        applied = _apply_to_existing(
            user_id, _criteria_to_query(criteria), add_ids, remove_ids
        )
        summary += f"; applied to {applied} existing email(s)"

    return summary
